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
    "PendingAction",
    "TimelineContractError",
    "TimelineEvent",
    "TimelineRecord",
    "TimelineResult",
    "feature_available_event",
    "market_observation_available_event",
    "process_timeline",
    "signal_available_event",
]
