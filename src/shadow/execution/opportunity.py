"""Stable causal source-opportunity identity for the future durable journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from shadow.domain import Instrument
from shadow.execution.journal_codec import canonical_digest
from shadow.features import FeatureInput, FeatureName, FeatureSnapshot
from shadow.risk.models import OrderIntent
from shadow.strategies import Signal


class SourceOpportunityError(ValueError):
    """A source key or its first immutable binding is inconsistent."""


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SourceOpportunityError(f"{field} must be a nonempty trimmed string")


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not UTC:
        raise SourceOpportunityError(f"{field} must use datetime.UTC")
    return value


@dataclass(frozen=True, slots=True)
class SourceOpportunityKey:
    """Identity of one completed source bar, unaffected by delivery mechanics."""

    account_id: str
    operational_scope: str
    feed_lineage: str
    instrument: Instrument
    completed_bar_observation_time: datetime
    strategy_id: str
    strategy_version: str
    feature_name: FeatureName
    feature_input: FeatureInput
    feature_implementation_version: str
    feature_window: int

    def __post_init__(self) -> None:
        for name in (
            "account_id",
            "operational_scope",
            "feed_lineage",
            "strategy_id",
            "strategy_version",
            "feature_implementation_version",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.instrument, Instrument):
            raise SourceOpportunityError("instrument must be an Instrument")
        object.__setattr__(
            self,
            "completed_bar_observation_time",
            _utc(self.completed_bar_observation_time, "completed_bar_observation_time"),
        )
        if not isinstance(self.feature_name, FeatureName) or not isinstance(
            self.feature_input, FeatureInput
        ):
            raise SourceOpportunityError("feature identity is invalid")
        if isinstance(self.feature_window, bool) or not isinstance(self.feature_window, int):
            raise SourceOpportunityError("feature_window must be a positive integer")
        if self.feature_window <= 0:
            raise SourceOpportunityError("feature_window must be a positive integer")

    @property
    def source_key(self) -> str:
        return canonical_digest(
            (
                "shadow.source-opportunity.v1",
                self.account_id,
                self.operational_scope,
                self.feed_lineage,
                self.instrument,
                self.completed_bar_observation_time,
                self.strategy_id,
                self.strategy_version,
                self.feature_name,
                self.feature_input,
                self.feature_implementation_version,
                self.feature_window,
            )
        )


@dataclass(frozen=True, slots=True)
class SourceOpportunityBinding:
    """The first feature/signal/intent evidence associated with a source key."""

    key: SourceOpportunityKey
    feature: FeatureSnapshot
    signal: Signal
    intent: OrderIntent

    def __post_init__(self) -> None:
        if not isinstance(self.key, SourceOpportunityKey):
            raise SourceOpportunityError("key must be a SourceOpportunityKey")
        if not isinstance(self.feature, FeatureSnapshot) or not isinstance(self.signal, Signal):
            raise SourceOpportunityError("feature and signal must retain domain contracts")
        if not isinstance(self.intent, OrderIntent):
            raise SourceOpportunityError("intent must retain the OrderIntent contract")
        if self.intent.source_signal != self.signal:
            raise SourceOpportunityError("intent source signal does not match binding")
        key = self.key
        if (
            self.feature.instrument != key.instrument
            or self.signal.instrument != key.instrument
            or self.intent.instrument != key.instrument
            or self.feature.source_dataset_id != key.feed_lineage
            or self.signal.source_dataset_id != key.feed_lineage
            or self.intent.operational_scope != key.operational_scope
        ):
            raise SourceOpportunityError("source binding instrument, lineage, or scope mismatch")
        if (
            self.feature.observation_time != key.completed_bar_observation_time
            or self.signal.feature_observation_time != key.completed_bar_observation_time
            or self.feature.availability_time != self.signal.feature_availability_time
            or self.feature.feature_name is not key.feature_name
            or self.feature.input_value is not key.feature_input
            or self.feature.implementation_version != key.feature_implementation_version
            or self.feature.window != key.feature_window
            or self.feature.value != self.signal.observed_feature_value
        ):
            raise SourceOpportunityError("source binding feature identity mismatch")
        if (
            self.signal.strategy_id != key.strategy_id
            or self.signal.strategy_version != key.strategy_version
            or self.signal.feature_name is not key.feature_name
            or self.signal.feature_input is not key.feature_input
            or self.signal.feature_implementation_version != key.feature_implementation_version
            or self.signal.feature_window != key.feature_window
        ):
            raise SourceOpportunityError("source binding signal identity mismatch")

    @property
    def evidence_digest(self) -> str:
        return canonical_digest(
            ("shadow.source-opportunity-binding.v1", self.feature, self.signal, self.intent)
        )


class SourceOpportunityRegistry:
    """In-memory conflict detector; P5A.2B will persist these first bindings."""

    def __init__(self) -> None:
        self._bindings: dict[str, SourceOpportunityBinding] = {}

    def bind(self, candidate: SourceOpportunityBinding) -> SourceOpportunityBinding:
        if not isinstance(candidate, SourceOpportunityBinding):
            raise SourceOpportunityError("candidate must be a SourceOpportunityBinding")
        source_key = candidate.key.source_key
        existing = self._bindings.get(source_key)
        if existing is None:
            self._bindings[source_key] = candidate
            return candidate
        if existing.evidence_digest != candidate.evidence_digest:
            raise SourceOpportunityError("materially changed evidence for source opportunity")
        return existing


def source_opportunity_key_for_intent(
    *, account_id: str, operational_scope: str, intent: OrderIntent
) -> SourceOpportunityKey:
    """Derive the stable causal key represented by one existing order intent.

    This deliberately uses source-bar and strategy evidence only.  Delivery time,
    quote receipt time, session IDs, and mutable display labels are not inputs.
    """
    if not isinstance(intent, OrderIntent):
        raise SourceOpportunityError("intent must be an OrderIntent")
    if intent.operational_scope != operational_scope:
        raise SourceOpportunityError("intent scope does not match source opportunity scope")
    signal = intent.source_signal
    return SourceOpportunityKey(
        account_id=account_id,
        operational_scope=operational_scope,
        feed_lineage=signal.source_dataset_id,
        instrument=intent.instrument,
        completed_bar_observation_time=signal.feature_observation_time,
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        feature_name=signal.feature_name,
        feature_input=signal.feature_input,
        feature_implementation_version=signal.feature_implementation_version,
        feature_window=signal.feature_window,
    )
