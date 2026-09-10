"""Deterministic chronological eligibility contracts for research simulation."""

from shadow.simulation.events import (
    EventKind,
    ExecutionOpportunity,
    TimelineContractError,
    TimelineEvent,
    feature_available_event,
    market_observation_available_event,
    signal_available_event,
)
from shadow.simulation.lifecycle import (
    InstrumentLifecycleState,
    LifecycleAction,
    LifecycleActionType,
    LifecycleContractError,
    LifecycleDecision,
    LifecycleReason,
    LifecycleRecord,
    LifecycleResult,
    LifecycleState,
    SimulatedPosition,
    process_lifecycle,
)
from shadow.simulation.timeline import (
    EligibilityDecision,
    EligibilityReason,
    PendingAction,
    TimelineRecord,
    TimelineResult,
    process_timeline,
)

__all__ = [
    "EligibilityDecision",
    "EligibilityReason",
    "EventKind",
    "ExecutionOpportunity",
    "InstrumentLifecycleState",
    "LifecycleAction",
    "LifecycleActionType",
    "LifecycleContractError",
    "LifecycleDecision",
    "LifecycleReason",
    "LifecycleRecord",
    "LifecycleResult",
    "LifecycleState",
    "PendingAction",
    "TimelineContractError",
    "TimelineEvent",
    "TimelineRecord",
    "TimelineResult",
    "SimulatedPosition",
    "feature_available_event",
    "market_observation_available_event",
    "process_timeline",
    "process_lifecycle",
    "signal_available_event",
]
