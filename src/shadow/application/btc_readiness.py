"""Deterministic historical feasibility, without forecasting or execution authority."""

from dataclasses import dataclass
from enum import StrEnum

from shadow.domain.crypto_market import UtcNanoseconds
from shadow.features.btc_trend import CompletedBtcInterval
from shadow.strategies.btc_trend import BtcTrendConfig


class ReadinessReason(StrEnum):
    READY = "READY"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MISSING_INTERVAL_CLOSE = "MISSING_INTERVAL_CLOSE"
    NONCONTIGUOUS_HISTORY = "NONCONTIGUOUS_HISTORY"


@dataclass(frozen=True, slots=True)
class BtcStrategyReadiness:
    ready: bool
    total_completed_intervals: int
    required_interval_count: int
    missing_close_intervals: tuple[tuple[int, int], ...]
    continuity_failures: tuple[tuple[int, int], ...]
    reason: ReadinessReason
    assessed_at_ns: int
    deadline_ns: int
    earliest_possible_ready_ns: int
    can_become_ready: bool


def required_btc_interval_count(config: BtcTrendConfig) -> int:
    """Completed intervals required by the unchanged trend feature contract."""
    return max(
        config.minimum_history,
        config.slow_horizon_ns // config.interval_ns,
        config.fast_horizon_ns // config.interval_ns + 1,
        config.volatility_horizon_ns // config.interval_ns + 1,
    )


def assess_btc_readiness(
    intervals: tuple[CompletedBtcInterval, ...],
    *,
    config: BtcTrendConfig,
    as_of_ns: int,
    deadline_ns: int,
) -> BtcStrategyReadiness:
    """Inspect the feature window and bound recovery using its valid suffix.

    New closes are hypothetical counts, never prices or feature inputs. Assume
    ideal future trade delivery, including immediate closure of a pending
    historical interval. This is a lower bound, not a promise of readiness.
    The existing live-trigger and risk checks remain independent.
    """
    for value in (as_of_ns, deadline_ns):
        UtcNanoseconds(value)
    step = config.interval_ns
    count = required_btc_interval_count(config)
    window = intervals[-count:]
    missing = tuple((r.start_ns, r.end_ns) for r in window if r.close is None)
    failures = tuple((r.start_ns, r.end_ns) for r in window if r.end_ns - r.start_ns != step)
    failures += tuple(
        (a.end_ns, b.start_ns)
        for a, b in zip(window, window[1:], strict=False)
        if a.end_ns != b.start_ns
    )
    ready = config.features(intervals, as_of_ns) is not None
    reason = (
        ReadinessReason.READY
        if ready
        else ReadinessReason.NONCONTIGUOUS_HISTORY
        if failures
        else ReadinessReason.MISSING_INTERVAL_CLOSE
        if missing
        else ReadinessReason.INSUFFICIENT_HISTORY
    )
    # Availability is retained in the lower bound, including future-dated rows;
    # it is never relaxed in the actual feature evaluator.
    anchor = window[-1].end_ns if window else as_of_ns // step * step
    earliest = max(as_of_ns, anchor + count * step)
    suffix = 0
    available = as_of_ns
    next_start = None
    for row in reversed(window):
        if (
            row.close is None
            or row.end_ns - row.start_ns != step
            or (next_start is not None and row.end_ns != next_start)
        ):
            break
        suffix += 1
        available = max(available, row.available_ns)
        next_start = row.start_ns
        earliest = min(earliest, max(as_of_ns, available, anchor + (count - suffix) * step))
    return BtcStrategyReadiness(
        ready,
        len(intervals),
        count,
        missing,
        failures,
        reason,
        as_of_ns,
        deadline_ns,
        earliest,
        earliest < deadline_ns,
    )
