"""Deterministic chronological eligibility processing without fill mechanics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shadow.domain import Instrument
from shadow.simulation.events import EventKind, TimelineContractError, TimelineEvent


class EligibilityDecision(StrEnum):
    """The state recorded for an event/action relationship."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class EligibilityReason(StrEnum):
    """A structured explanation for the timeline record's decision."""

    EVENT_PROCESSED = "event_processed"
    SIGNAL_AWAITING_STRICTLY_LATER_OPPORTUNITY = "signal_awaiting_strictly_later_opportunity"
    OPPORTUNITY_NOT_STRICTLY_AFTER_SIGNAL_AVAILABILITY = (
        "opportunity_not_strictly_after_signal_availability"
    )
    LEGAL_EXECUTION_OPPORTUNITY = "legal_execution_opportunity"


@dataclass(frozen=True, slots=True)
class PendingAction:
    """A signal whose opportunities must remain strictly later than its availability.

    ``eligibility_after_time`` is an exclusive boundary: a qualifying execution
    event must have ``event_time > eligibility_after_time``.  P0.5A leaves this
    evidence pending because only an explicit execution outcome may consume a
    lifecycle action.  It is intentionally not a broker order or risk authorization.
    """

    signal_reference: str
    instrument: Instrument
    signal_availability_time: datetime
    eligibility_after_time: datetime


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    """Immutable evidence of one processed event or eligibility determination."""

    event_reference: str
    event_kind: EventKind
    event_time: datetime
    source_reference: str
    signal_reference: str | None
    signal_availability_time: datetime | None
    execution_event_reference: str | None
    execution_event_time: datetime | None
    eligibility_decision: EligibilityDecision
    reason: EligibilityReason


@dataclass(frozen=True, slots=True)
class TimelineResult:
    """Canonical events, reconstructable trace, and still-open eligibility evidence."""

    ordered_events: tuple[TimelineEvent, ...]
    records: tuple[TimelineRecord, ...]
    pending_actions: tuple[PendingAction, ...]


def _ordered_events(events: Iterable[TimelineEvent]) -> tuple[TimelineEvent, ...]:
    materialized = tuple(events)
    if any(not isinstance(event, TimelineEvent) for event in materialized):
        raise TimelineContractError("events must contain only TimelineEvent values")
    identifiers = [event.event_id for event in materialized]
    if len(identifiers) != len(set(identifiers)):
        raise TimelineContractError("event_id values must be unique within a timeline")
    return tuple(sorted(materialized, key=lambda event: event.sort_key))


def _event_record(event: TimelineEvent) -> TimelineRecord:
    return TimelineRecord(
        event_reference=event.event_id,
        event_kind=event.kind,
        event_time=event.effective_time,
        source_reference=event.source_reference,
        signal_reference=None,
        signal_availability_time=None,
        execution_event_reference=None,
        execution_event_time=None,
        eligibility_decision=EligibilityDecision.NOT_APPLICABLE,
        reason=EligibilityReason.EVENT_PROCESSED,
    )


def process_timeline(events: Iterable[TimelineEvent]) -> TimelineResult:
    """Process a fixed event set into strictly-later eligibility evidence.

    The legal rule is strict: a signal can be eligible only at an execution
    opportunity for the same instrument whose event time is later than the signal's
    availability time.  Equal timestamps remain ineligible even though their
    documented event precedence places signals before opportunities.  Every later
    opportunity remains chronologically legal; P0.5A determines whether a pending
    lifecycle action actually attempts it and only a fill may consume that action.
    No fill, price, position, risk, or conflict resolution is implied.
    """
    ordered_events = _ordered_events(events)
    pending: list[PendingAction] = []
    records: list[TimelineRecord] = []

    for event in ordered_events:
        if event.kind is EventKind.SIGNAL_AVAILABLE:
            assert event.signal is not None  # guaranteed by TimelineEvent's contract
            action = PendingAction(
                signal_reference=event.event_id,
                instrument=event.instrument,
                signal_availability_time=event.signal.availability_time,
                eligibility_after_time=event.signal.availability_time,
            )
            pending.append(action)
            records.append(
                TimelineRecord(
                    event_reference=event.event_id,
                    event_kind=event.kind,
                    event_time=event.effective_time,
                    source_reference=event.source_reference,
                    signal_reference=action.signal_reference,
                    signal_availability_time=action.signal_availability_time,
                    execution_event_reference=None,
                    execution_event_time=None,
                    eligibility_decision=EligibilityDecision.PENDING,
                    reason=EligibilityReason.SIGNAL_AWAITING_STRICTLY_LATER_OPPORTUNITY,
                )
            )
            continue

        if event.kind is not EventKind.EXECUTION_OPPORTUNITY:
            records.append(_event_record(event))
            continue

        matching = [action for action in pending if action.instrument == event.instrument]
        if not matching:
            records.append(_event_record(event))
            continue

        for action in matching:
            eligible = event.effective_time > action.eligibility_after_time
            decision = EligibilityDecision.ELIGIBLE if eligible else EligibilityDecision.INELIGIBLE
            reason = (
                EligibilityReason.LEGAL_EXECUTION_OPPORTUNITY
                if eligible
                else EligibilityReason.OPPORTUNITY_NOT_STRICTLY_AFTER_SIGNAL_AVAILABILITY
            )
            records.append(
                TimelineRecord(
                    event_reference=event.event_id,
                    event_kind=event.kind,
                    event_time=event.effective_time,
                    source_reference=event.source_reference,
                    signal_reference=action.signal_reference,
                    signal_availability_time=action.signal_availability_time,
                    execution_event_reference=event.event_id,
                    execution_event_time=event.effective_time,
                    eligibility_decision=decision,
                    reason=reason,
                )
            )

    return TimelineResult(
        ordered_events=ordered_events,
        records=tuple(records),
        pending_actions=tuple(pending),
    )
