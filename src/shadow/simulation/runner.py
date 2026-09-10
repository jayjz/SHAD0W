"""Deterministic end-to-end composition for the initial research hypothesis.

The runner advances strategy decisions by configured feature-availability
instants.  It intentionally delegates feature calculation, strategy rules,
timeline eligibility, and lifecycle transitions to their existing components.
An execution opportunity remains a chronological permission; the P0.5A/P0.5B
model supplies the separate explicit outcome that may change lifecycle state.
P0.5C economics is attached only after that authoritative outcome is resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shadow.data import dataset_fingerprint, validate_bars
from shadow.domain import Bar, DatasetMetadata, Instrument, Quote, ValidationStatus
from shadow.execution import (
    EconomicExecution,
    ExecutionAttempt,
    ExecutionEconomicsConfig,
    ExecutionOutcome,
    QuoteExecutionConfig,
    attach_execution_economics,
    quote_reference,
)
from shadow.features import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureSnapshot,
    FeatureState,
    calculate_z_scores,
)
from shadow.simulation.events import (
    ExecutionOpportunity,
    TimelineEvent,
    feature_available_event,
    market_observation_available_event,
    signal_available_event,
)
from shadow.simulation.lifecycle import (
    LifecycleResult,
    LifecycleState,
    process_lifecycle,
)
from shadow.simulation.timeline import TimelineResult, process_timeline
from shadow.strategies import (
    MeanReversionConfig,
    PositionState,
    Signal,
    evaluate_mean_reversion,
)

SIMULATION_IMPLEMENTATION_VERSION = "shadow.simulation.runner.v3"


class SimulationContractError(ValueError):
    """An invalid simulation runner input or inconsistent runner evidence."""


class StrategyEvaluationDisposition(StrEnum):
    """The explicit outcome of one intended availability-driven decision point."""

    FEATURE_NOT_ACTIONABLE = "feature_not_actionable"
    PENDING_LIFECYCLE = "pending_lifecycle"
    NO_SIGNAL = "no_signal"
    SIGNAL_PROPOSED = "signal_proposed"


@dataclass(frozen=True, slots=True)
class SimulationInput:
    """Immutable inputs for one initial mean-reversion simulation run.

    ``bars`` must retain P0.1's caller order so validation can reject invalid
    chronology.  A mean-reversion configuration supplies the corresponding
    z-score feature window and identity; there is deliberately no separate
    universal feature configuration.  At most one configuration is accepted
    for an instrument because P0.4B owns one lifecycle per instrument. Optional
    economics configs are exact-coverage downstream assumptions and are placed
    last to preserve the existing positional price-only construction contract.
    """

    dataset_metadata: DatasetMetadata
    bars: tuple[Bar, ...]
    strategy_configs: tuple[MeanReversionConfig, ...]
    execution_config: QuoteExecutionConfig
    quotes: tuple[Quote, ...] = ()
    execution_opportunities: tuple[ExecutionOpportunity, ...] = ()
    simulation_id: str | None = None
    economics_configs: tuple[ExecutionEconomicsConfig, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_metadata, DatasetMetadata):
            raise SimulationContractError("dataset_metadata must be a DatasetMetadata")
        for field_name, value, expected_type in (
            ("bars", self.bars, Bar),
            ("strategy_configs", self.strategy_configs, MeanReversionConfig),
            ("quotes", self.quotes, Quote),
            ("execution_opportunities", self.execution_opportunities, ExecutionOpportunity),
        ):
            if not isinstance(value, tuple) or not all(
                isinstance(item, expected_type) for item in value
            ):
                raise SimulationContractError(
                    f"{field_name} must be a tuple of {expected_type.__name__} values"
                )
        if not isinstance(self.execution_config, QuoteExecutionConfig):
            raise SimulationContractError("execution_config must be a QuoteExecutionConfig")
        if not self.strategy_configs:
            raise SimulationContractError(
                "strategy_configs must contain at least one configuration"
            )
        instruments = [config.instrument for config in self.strategy_configs]
        if len(instruments) != len(set(instruments)):
            raise SimulationContractError(
                "strategy_configs must contain at most one per instrument"
            )
        if any(instrument not in self.dataset_metadata.instruments for instrument in instruments):
            raise SimulationContractError(
                "strategy configuration instrument is absent from dataset scope"
            )
        if self.economics_configs is not None:
            if not isinstance(self.economics_configs, tuple) or not all(
                isinstance(config, ExecutionEconomicsConfig) for config in self.economics_configs
            ):
                raise SimulationContractError(
                    "economics_configs must be a tuple of ExecutionEconomicsConfig values or None"
                )
            economics_instruments = [config.instrument for config in self.economics_configs]
            if len(economics_instruments) != len(set(economics_instruments)):
                raise SimulationContractError(
                    "economics_configs must contain exactly one per configured instrument; "
                    "duplicates are not allowed"
                )
            configured_instruments = set(instruments)
            supplied_instruments = set(economics_instruments)
            if supplied_instruments != configured_instruments:
                missing = sorted(
                    instrument.identifier
                    for instrument in configured_instruments - supplied_instruments
                )
                extra = sorted(
                    instrument.identifier
                    for instrument in supplied_instruments - configured_instruments
                )
                details = []
                if missing:
                    details.append(f"missing={missing!r}")
                if extra:
                    details.append(f"extra={extra!r}")
                raise SimulationContractError(
                    "economics_configs must cover configured instruments exactly ("
                    + ", ".join(details)
                    + ")"
                )
            object.__setattr__(
                self,
                "economics_configs",
                tuple(
                    sorted(
                        self.economics_configs,
                        key=lambda config: config.instrument.identifier,
                    )
                ),
            )
        if self.simulation_id is not None and (
            not self.simulation_id or self.simulation_id != self.simulation_id.strip()
        ):
            raise SimulationContractError(
                "simulation_id must be a non-empty trimmed string or None"
            )


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    """Structured evidence for one selected snapshot at a decision instant."""

    snapshot: FeatureSnapshot
    decision_time: datetime
    lifecycle_state: LifecycleState
    position_state: PositionState | None
    disposition: StrategyEvaluationDisposition
    signal: Signal | None
    signal_event_reference: str | None

    def __post_init__(self) -> None:
        if self.decision_time != self.snapshot.availability_time:
            raise SimulationContractError("decision_time must equal snapshot availability_time")
        if not isinstance(self.lifecycle_state, LifecycleState):
            raise SimulationContractError("lifecycle_state must be a LifecycleState")
        if self.position_state is not None and not isinstance(self.position_state, PositionState):
            raise SimulationContractError("position_state must be a PositionState or None")
        if not isinstance(self.disposition, StrategyEvaluationDisposition):
            raise SimulationContractError("disposition must be a StrategyEvaluationDisposition")
        proposed = self.disposition is StrategyEvaluationDisposition.SIGNAL_PROPOSED
        if proposed != (self.signal is not None and self.signal_event_reference is not None):
            raise SimulationContractError("proposed disposition must agree with signal evidence")
        if self.disposition is StrategyEvaluationDisposition.PENDING_LIFECYCLE and (
            self.lifecycle_state not in {LifecycleState.PENDING_ENTRY, LifecycleState.PENDING_EXIT}
        ):
            raise SimulationContractError("pending disposition requires a pending lifecycle state")
        if (
            self.disposition
            in {
                StrategyEvaluationDisposition.NO_SIGNAL,
                StrategyEvaluationDisposition.SIGNAL_PROPOSED,
            }
            and self.position_state is None
        ):
            raise SimulationContractError("evaluated strategy evidence requires a position state")


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Immutable chronological, execution, and optional economics evidence."""

    simulation_implementation_version: str
    simulation_id: str | None
    dataset_metadata: DatasetMetadata
    dataset_fingerprint: str
    strategy_configs: tuple[MeanReversionConfig, ...]
    execution_config: QuoteExecutionConfig
    quotes: tuple[Quote, ...]
    feature_implementation_version: str
    features: tuple[FeatureSnapshot, ...]
    evaluations: tuple[StrategyEvaluation, ...]
    signals: tuple[Signal, ...]
    timeline: TimelineResult
    lifecycle: LifecycleResult
    execution_attempts: tuple[ExecutionAttempt, ...]
    execution_outcomes: tuple[ExecutionOutcome, ...]
    economics_configs: tuple[ExecutionEconomicsConfig, ...] | None = None
    economic_executions: tuple[EconomicExecution, ...] = ()


def _time_token(value: datetime) -> str:
    """Return an explicit, stable UTC timestamp fragment for generated event IDs."""
    return value.isoformat(timespec="microseconds")


def _market_event_id(bar: Bar) -> str:
    return (
        f"market:{bar.instrument.identifier}:{bar.interval.microseconds}:"
        f"{_time_token(bar.observation_time)}"
    )


def _feature_event_id(snapshot: FeatureSnapshot) -> str:
    return (
        f"feature:{snapshot.instrument.identifier}:{snapshot.feature_name.value}:"
        f"{snapshot.window}:{_time_token(snapshot.observation_time)}:"
        f"{snapshot.source_dataset_id}"
    )


def _signal_event_id(signal: Signal) -> str:
    return (
        f"signal:{signal.instrument.identifier}:{signal.configuration_id}:"
        f"{_time_token(signal.feature_observation_time)}"
    )


def _feature_sort_key(snapshot: FeatureSnapshot) -> tuple[datetime, str, datetime, int]:
    """Order evidence canonically without depending on caller bar/config order."""
    return (
        snapshot.availability_time,
        snapshot.instrument.identifier,
        snapshot.observation_time,
        snapshot.window,
    )


def _configured_features(simulation_input: SimulationInput) -> tuple[FeatureSnapshot, ...]:
    """Reuse the P0.2 z-score kernel for exactly the configured instruments/windows."""
    snapshots: list[FeatureSnapshot] = []
    configs_by_window: dict[int, tuple[MeanReversionConfig, ...]] = {}
    for config in simulation_input.strategy_configs:
        configs_by_window[config.rolling_window] = (
            *configs_by_window.get(config.rolling_window, ()),
            config,
        )

    for window in sorted(configs_by_window):
        configured_instruments = {config.instrument for config in configs_by_window[window]}
        snapshots.extend(
            snapshot
            for snapshot in calculate_z_scores(
                simulation_input.bars, simulation_input.dataset_metadata, window=window
            )
            if snapshot.instrument in configured_instruments
        )
    return tuple(sorted(snapshots, key=_feature_sort_key))


def _decision_snapshots(features: tuple[FeatureSnapshot, ...]) -> tuple[FeatureSnapshot, ...]:
    """Choose one newest legally available snapshot per instrument/time instant.

    Delayed market delivery can make several historical snapshots available at
    the same instant.  That instant is one strategy decision point, not a
    backfill loop that emits multiple actions.  Selecting the greatest aligned
    observation time is causal because every candidate has the same already
    legal availability time; it never reaches forward into a later instant.
    """
    latest: dict[tuple[Instrument, datetime], FeatureSnapshot] = {}
    for snapshot in features:
        key = (snapshot.instrument, snapshot.availability_time)
        current = latest.get(key)
        if current is None or snapshot.observation_time > current.observation_time:
            latest[key] = snapshot
    return tuple(sorted(latest.values(), key=_feature_sort_key))


def _state_before_decision(
    signal_events: tuple[TimelineEvent, ...],
    opportunity_events: tuple[TimelineEvent, ...],
    *,
    decision_time: datetime,
    instrument: Instrument,
    execution_config: QuoteExecutionConfig,
    quotes: tuple[Quote, ...],
) -> LifecycleState:
    """Return P0.4B state immediately before a feature-time strategy decision.

    P0.4A orders all feature events before signals and opportunities at an equal
    timestamp.  Therefore only previously emitted signals and opportunities at
    *strictly earlier* times can affect the strategy context for this decision.
    Market and feature events have no lifecycle transition of their own, so they
    need not be replayed to obtain the same authoritative state.
    """
    prior_events = (
        *(event for event in signal_events if event.effective_time < decision_time),
        *(event for event in opportunity_events if event.effective_time < decision_time),
    )
    return (
        process_lifecycle(prior_events, execution_config=execution_config, quotes=quotes)
        .state_for(instrument)
        .state
    )


def _position_state(lifecycle_state: LifecycleState) -> PositionState | None:
    if lifecycle_state is LifecycleState.FLAT:
        return PositionState.FLAT
    if lifecycle_state is LifecycleState.HOLDING:
        return PositionState.HOLDING
    return None


def run_simulation(simulation_input: SimulationInput) -> SimulationResult:
    """Run the current hypothesis through P0.1--P0.5C deterministic evidence.

    The explicit decision rule is one strategy evaluation per configured
    instrument/availability instant, using that instant's newest available z-score
    snapshot.  The full dataset may be present in memory for P0.2's prefix-stable
    calculation, but a snapshot is exposed and evaluated only at its legal time;
    signals are generated during this chronological pass rather than precomputed
    for the complete dataset.
    """
    if not isinstance(simulation_input, SimulationInput):
        raise SimulationContractError("simulation_input must be a SimulationInput")

    # Preserve P0.1 failure semantics: reject bad caller order rather than sorting it.
    validate_bars(simulation_input.bars, simulation_input.dataset_metadata)
    if simulation_input.dataset_metadata.validation_status is not ValidationStatus.VALIDATED:
        raise SimulationContractError("dataset_metadata must declare validated status")

    canonical_configs = tuple(
        sorted(simulation_input.strategy_configs, key=lambda config: config.instrument.identifier)
    )
    canonical_quotes = tuple(
        sorted(
            simulation_input.quotes,
            key=lambda quote: (
                quote.availability_time,
                quote.observation_time,
                quote_reference(quote),
            ),
        )
    )
    config_by_instrument = {config.instrument: config for config in canonical_configs}
    features = _configured_features(simulation_input)
    decision_snapshots = _decision_snapshots(features)

    market_events = tuple(
        market_observation_available_event(bar, event_id=_market_event_id(bar))
        for bar in simulation_input.bars
    )
    feature_events = tuple(
        feature_available_event(snapshot, event_id=_feature_event_id(snapshot))
        for snapshot in features
    )
    opportunity_events = tuple(
        opportunity.as_event() for opportunity in simulation_input.execution_opportunities
    )

    signal_events: list[TimelineEvent] = []
    evaluations: list[StrategyEvaluation] = []
    for snapshot in decision_snapshots:
        lifecycle_state = _state_before_decision(
            tuple(signal_events),
            opportunity_events,
            decision_time=snapshot.availability_time,
            instrument=snapshot.instrument,
            execution_config=simulation_input.execution_config,
            quotes=canonical_quotes,
        )
        if snapshot.state is not FeatureState.READY:
            evaluations.append(
                StrategyEvaluation(
                    snapshot=snapshot,
                    decision_time=snapshot.availability_time,
                    lifecycle_state=lifecycle_state,
                    position_state=None,
                    disposition=StrategyEvaluationDisposition.FEATURE_NOT_ACTIONABLE,
                    signal=None,
                    signal_event_reference=None,
                )
            )
            continue

        position_state = _position_state(lifecycle_state)
        if position_state is None:
            evaluations.append(
                StrategyEvaluation(
                    snapshot=snapshot,
                    decision_time=snapshot.availability_time,
                    lifecycle_state=lifecycle_state,
                    position_state=None,
                    disposition=StrategyEvaluationDisposition.PENDING_LIFECYCLE,
                    signal=None,
                    signal_event_reference=None,
                )
            )
            continue

        signal = evaluate_mean_reversion(
            snapshot,
            config_by_instrument[snapshot.instrument],
            position_state,
            decision_time=snapshot.availability_time,
        )
        if signal is None:
            evaluations.append(
                StrategyEvaluation(
                    snapshot=snapshot,
                    decision_time=snapshot.availability_time,
                    lifecycle_state=lifecycle_state,
                    position_state=position_state,
                    disposition=StrategyEvaluationDisposition.NO_SIGNAL,
                    signal=None,
                    signal_event_reference=None,
                )
            )
            continue

        event_id = _signal_event_id(signal)
        signal_events.append(signal_available_event(signal, event_id=event_id))
        evaluations.append(
            StrategyEvaluation(
                snapshot=snapshot,
                decision_time=snapshot.availability_time,
                lifecycle_state=lifecycle_state,
                position_state=position_state,
                disposition=StrategyEvaluationDisposition.SIGNAL_PROPOSED,
                signal=signal,
                signal_event_reference=event_id,
            )
        )

    all_events = (*market_events, *feature_events, *signal_events, *opportunity_events)
    timeline = process_timeline(all_events)
    lifecycle = process_lifecycle(
        all_events,
        execution_config=simulation_input.execution_config,
        quotes=canonical_quotes,
    )
    economic_executions: tuple[EconomicExecution, ...] = ()
    if simulation_input.economics_configs is not None:
        economics_by_instrument = {
            config.instrument: config for config in simulation_input.economics_configs
        }
        attached = (
            attach_execution_economics(
                outcome,
                economics_by_instrument[outcome.attempt.instrument],
            )
            for outcome in lifecycle.execution_outcomes
        )
        economic_executions = tuple(evidence for evidence in attached if evidence is not None)
    return SimulationResult(
        simulation_implementation_version=SIMULATION_IMPLEMENTATION_VERSION,
        simulation_id=simulation_input.simulation_id,
        dataset_metadata=simulation_input.dataset_metadata,
        dataset_fingerprint=dataset_fingerprint(
            simulation_input.bars, simulation_input.dataset_metadata
        ),
        strategy_configs=canonical_configs,
        execution_config=simulation_input.execution_config,
        quotes=canonical_quotes,
        feature_implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        features=features,
        evaluations=tuple(evaluations),
        signals=tuple(event.signal for event in signal_events if event.signal is not None),
        timeline=timeline,
        lifecycle=lifecycle,
        execution_attempts=lifecycle.execution_attempts,
        execution_outcomes=lifecycle.execution_outcomes,
        economics_configs=simulation_input.economics_configs,
        economic_executions=economic_executions,
    )
