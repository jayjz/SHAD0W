"""Read-only reconciliation of complete simulation evidence into long trades."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shadow.domain import Instrument
from shadow.evaluation.models import EvaluationError, TradeRecord, validate_fill
from shadow.execution import (
    EconomicExecution,
    ExecutionActionType,
    ExecutionOutcome,
    ExecutionStatus,
)
from shadow.simulation.events import EventKind
from shadow.simulation.lifecycle import (
    InstrumentLifecycleState,
    LifecycleAction,
    LifecycleDecision,
    LifecycleRecord,
    LifecycleState,
    SimulatedPosition,
)
from shadow.simulation.runner import SimulationResult
from shadow.strategies import SignalType


def unique[T](items: tuple[T, ...], key: Callable[[T], str], label: str) -> dict[str, T]:
    """Reject duplicates even when byte-identical; omissions must remain detectable."""
    if not isinstance(items, tuple):
        raise EvaluationError(f"{label} must be immutable tuple evidence")
    result = {key(item): item for item in items}
    if len(result) != len(items):
        raise EvaluationError(f"duplicate {label}")
    return result


@dataclass(frozen=True, slots=True)
class TradeReconstructionResult:
    completed_trades: tuple[TradeRecord, ...]
    incomplete_states: tuple[InstrumentLifecycleState, ...]
    open_entries: tuple[EconomicExecution, ...]
    unsuccessful_outcomes: tuple[ExecutionOutcome, ...]

    @property
    def incomplete_trade_count(self) -> int:
        """One unresolved lifecycle per instrument, including an unfilled pending entry."""
        return len(self.incomplete_states)


def reconstruct_trades(result: SimulationResult) -> TradeReconstructionResult:
    """Account for every execution and lifecycle event, or fail the entire reconstruction.

    Reconcile supplied state transitions; never rerun strategy or fill selection.
    An internally rewritten entire source run cannot be authenticated here: the
    manifest fingerprints the supplied evidence and the caller attests its origin.
    """
    if not isinstance(result, SimulationResult) or result.economics_configs is None:
        raise EvaluationError("evaluation requires SimulationResult with enabled economics")
    configs = unique(
        result.economics_configs, lambda c: c.instrument.identifier, "economics configs"
    )
    strategies = unique(
        result.strategy_configs, lambda c: c.instrument.identifier, "strategy configs"
    )
    if configs.keys() != strategies.keys():
        raise EvaluationError("economics configurations must cover strategies exactly")
    events = unique(result.timeline.ordered_events, lambda e: e.event_id, "events")
    if events != unique(result.lifecycle.ordered_events, lambda e: e.event_id, "lifecycle events"):
        raise EvaluationError("timeline and lifecycle events disagree")
    records = unique(result.lifecycle.records, lambda r: r.event_reference, "lifecycle records")
    if records.keys() != events.keys():
        raise EvaluationError("lifecycle records must cover every event exactly")
    attempts = unique(result.execution_attempts, lambda a: a.attempt_id, "attempts")
    outcomes = unique(result.execution_outcomes, lambda o: o.attempt.attempt_id, "outcomes")
    if attempts != unique(
        result.lifecycle.execution_attempts, lambda a: a.attempt_id, "lifecycle attempts"
    ):
        raise EvaluationError("simulation and lifecycle attempts disagree")
    if outcomes != unique(
        result.lifecycle.execution_outcomes, lambda o: o.attempt.attempt_id, "lifecycle outcomes"
    ):
        raise EvaluationError("simulation and lifecycle outcomes disagree")
    if attempts != {key: outcome.attempt for key, outcome in outcomes.items()}:
        raise EvaluationError("attempts and outcomes must have exact coverage")
    economics = unique(
        result.economic_executions, lambda e: e.attempt_reference, "economic executions"
    )
    filled = {key: o for key, o in outcomes.items() if o.status is ExecutionStatus.FILLED}
    if filled != {key: e.outcome for key, e in economics.items()}:
        raise EvaluationError("economic evidence must cover every filled outcome exactly")
    for economic in economics.values():
        if economic.config != configs.get(economic.instrument.identifier):
            raise EvaluationError("economic quantity/currency/fee configuration disagrees with run")

    states: dict[Instrument, InstrumentLifecycleState] = {}
    openings: dict[str, tuple[LifecycleRecord, EconomicExecution]] = {}
    used_attempts: set[str] = set()
    actions: set[str] = set()
    trades: list[TradeRecord] = []
    for event in sorted(events.values(), key=lambda e: e.sort_key):
        record = records[event.event_id]
        instrument = event.instrument
        current = states.setdefault(
            instrument, InstrumentLifecycleState(instrument, LifecycleState.FLAT, None, None)
        )
        if (
            record.instrument != instrument
            or record.event_kind != event.kind
            or record.event_time != event.effective_time
            or record.source_reference != event.source_reference
            or record.prior_state != current.state
        ):
            raise EvaluationError("lifecycle record disagrees with event or prior state")
        action = record.action
        outcome = record.execution_outcome
        if record.decision is LifecycleDecision.ACTION_CREATED:
            signal = event.signal
            if (
                signal is None
                or action is None
                or outcome is not None
                or record.execution_attempt is not None
                or action.action_id in actions
                or action.signal_reference != event.event_id
                or action.instrument != instrument
                or action.created_time != event.effective_time
                or action.origin_state != current.state
                or record.position != current.open_position
                or current.pending_action is not None
                or instrument.identifier not in strategies
                or action.action_type
                != (
                    ExecutionActionType.ENTRY
                    if signal.signal_type is SignalType.LONG_ENTRY
                    else ExecutionActionType.EXIT
                )
            ):
                raise EvaluationError("invalid or duplicate lifecycle action creation")
            actions.add(action.action_id)
            states[instrument] = InstrumentLifecycleState(
                instrument,
                LifecycleState.PENDING_ENTRY
                if action.action_type is ExecutionActionType.ENTRY
                else LifecycleState.PENDING_EXIT,
                action,
                current.open_position,
            )
        elif outcome is not None:
            attempt = outcome.attempt
            if (
                action is None
                or action != current.pending_action
                or record.execution_attempt != attempt
                or outcomes.get(attempt.attempt_id) != outcome
                or attempt.attempt_id in used_attempts
                or attempt.action_id != action.action_id
                or attempt.signal_reference != action.signal_reference
                or attempt.action_type != action.action_type
                or attempt.instrument != instrument
                or attempt.opportunity_instrument != instrument
                or attempt.opportunity_reference != event.event_id
                or attempt.attempt_time != event.effective_time
                or attempt.opportunity_time != event.effective_time
                or attempt.eligibility_after_time != action.eligibility_after_time
                or event.effective_time <= action.eligibility_after_time
                or event.kind is not EventKind.EXECUTION_OPPORTUNITY
                or outcome.execution_config != result.execution_config
                or attempt.execution_model_id != result.execution_config.execution_model_id
            ):
                raise EvaluationError("attempt does not match the pending action and opportunity")
            if outcome.market_evidence is not None and outcome.market_evidence not in result.quotes:
                raise EvaluationError("outcome quote is absent from supplied quote evidence")
            used_attempts.add(attempt.attempt_id)
            if outcome.status is ExecutionStatus.FILLED:
                economic = economics[attempt.attempt_id]
                if action.action_type is ExecutionActionType.ENTRY:
                    validate_fill(record, economic)
                    if current.open_position is not None or action.action_id in openings:
                        raise EvaluationError("entry already opened")
                    openings[action.action_id] = (record, economic)
                    states[instrument] = InstrumentLifecycleState(
                        instrument, LifecycleState.HOLDING, None, record.position
                    )
                else:
                    position = current.open_position
                    if position is None or record.position != position:
                        raise EvaluationError("exit must close the authoritative open position")
                    opening = openings.pop(position.opening_action_id, None)
                    if opening is None:
                        raise EvaluationError("exit has no unique filled opening action")
                    trades.append(TradeRecord(opening[1], economic, opening[0], record))
                    states[instrument] = InstrumentLifecycleState(
                        instrument, LifecycleState.FLAT, None, None
                    )
            elif (
                record.decision
                != (
                    LifecycleDecision.ATTEMPT_UNFILLED
                    if outcome.status is ExecutionStatus.UNFILLED
                    else LifecycleDecision.ATTEMPT_REJECTED
                )
                or record.position != current.open_position
            ):
                raise EvaluationError("unsuccessful attempt must retain its pending state")
        elif (
            record.execution_attempt is not None
            or record.decision
            in {
                LifecycleDecision.ACTION_FILLED,
                LifecycleDecision.ATTEMPT_UNFILLED,
                LifecycleDecision.ATTEMPT_REJECTED,
            }
            or (action is not None and action != current.pending_action)
            or (record.position is not None and record.position != current.open_position)
        ):
            raise EvaluationError("missing execution evidence or inconsistent non-transition")
        if record.resulting_state != states[instrument].state:
            raise EvaluationError("lifecycle resulting state disagrees with evidence")
    if used_attempts != attempts.keys():
        raise EvaluationError("orphan execution attempts")
    final = unique(result.lifecycle.final_states, lambda s: s.instrument.identifier, "final states")
    if final != {instrument.identifier: state for instrument, state in states.items()}:
        raise EvaluationError("final states disagree with lifecycle trace")
    expected_actions: tuple[LifecycleAction, ...] = tuple(
        s.pending_action for s in states.values() if s.pending_action is not None
    )
    expected_positions: tuple[SimulatedPosition, ...] = tuple(
        s.open_position for s in states.values() if s.open_position is not None
    )
    if unique(expected_actions, lambda a: a.action_id, "pending actions") != unique(
        result.lifecycle.unresolved_actions, lambda a: a.action_id, "unresolved actions"
    ) or unique(expected_positions, lambda p: p.opening_action_id, "positions") != unique(
        result.lifecycle.open_positions, lambda p: p.opening_action_id, "open positions"
    ):
        raise EvaluationError("unresolved actions/open positions disagree with final states")
    if set(openings) != {position.opening_action_id for position in expected_positions}:
        raise EvaluationError("filled entries must be completed or explicitly open")
    return TradeReconstructionResult(
        tuple(
            sorted(
                trades,
                key=lambda t: (t.exit_time, t.instrument.identifier, t.exit_action_reference),
            )
        ),
        tuple(
            sorted(
                (s for s in states.values() if s.state is not LifecycleState.FLAT),
                key=lambda s: s.instrument.identifier,
            )
        ),
        tuple(openings[key][1] for key in sorted(openings)),
        tuple(
            sorted(
                (o for o in outcomes.values() if o.status is not ExecutionStatus.FILLED),
                key=lambda o: o.attempt.sort_key,
            )
        ),
    )
