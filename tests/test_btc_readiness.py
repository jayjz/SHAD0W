"""Readiness uses actual completed closes and an optimistic recovery bound."""

from dataclasses import replace
from decimal import Decimal

import pytest

from shadow.application.btc_readiness import assess_btc_readiness
from shadow.features.btc_trend import HOUR_NS, CompletedBtcInterval
from shadow.strategies.btc_trend import engineering_canary


def rows() -> tuple[CompletedBtcInterval, ...]:
    return tuple(
        CompletedBtcInterval(i * HOUR_NS, (i + 1) * HOUR_NS, 100 * HOUR_NS, Decimal(100), str(i))
        for i in range(22, 100)
    )


@pytest.mark.parametrize(
    "case,reason,ready,possible",
    [
        ("clean", "READY", True, True),
        ("short", "INSUFFICIENT_HISTORY", False, False),
        ("missing", "MISSING_INTERVAL_CLOSE", False, False),
        ("gap", "NONCONTIGUOUS_HISTORY", False, False),
        ("width", "NONCONTIGUOUS_HISTORY", False, False),
        ("old_missing", "READY", True, True),
        ("aging", "MISSING_INTERVAL_CLOSE", False, True),
    ],
)
def test_readiness(case: str, reason: str, ready: bool, possible: bool) -> None:
    history = rows()
    if case == "short":
        history = history[-2:]
    elif case in ("missing", "old_missing", "aging"):
        index = {"missing": 74, "old_missing": 0, "aging": 6}[case]
        history = tuple(
            replace(row, close=None, closing_trade_id=None) if i == index else row
            for i, row in enumerate(history)
        )
    elif case == "gap":
        history = history[:-3] + history[-2:]
    elif case == "width":
        history = history[:-1] + (replace(history[-1], start_ns=history[-1].start_ns + 1),)
    result = assess_btc_readiness(
        history,
        config=engineering_canary(),
        as_of_ns=100 * HOUR_NS,
        deadline_ns=124 * HOUR_NS,
    )
    assert result.ready is ready
    assert result.reason == reason
    assert result.can_become_ready is possible
    assert result.total_completed_intervals == len(history)
    assert result.required_interval_count == 73
    assert bool(result.continuity_failures) == (case in ("gap", "width"))
    assert bool(result.missing_close_intervals) == (case in ("missing", "aging"))
    assert result == assess_btc_readiness(
        history,
        config=engineering_canary(),
        as_of_ns=100 * HOUR_NS,
        deadline_ns=124 * HOUR_NS,
    )


def test_recovery_at_deadline_is_too_late() -> None:
    history = rows()
    # Missing hour ending 51 ages out exactly at hour 124 (51 + 73).
    history = tuple(
        replace(row, close=None, closing_trade_id=None) if row.end_ns == 51 * HOUR_NS else row
        for row in history
    )
    config = engineering_canary()
    result = assess_btc_readiness(
        history, deadline_ns=124 * HOUR_NS, config=config, as_of_ns=100 * HOUR_NS
    )
    assert result.earliest_possible_ready_ns == 124 * HOUR_NS
    assert not result.can_become_ready
    assert assess_btc_readiness(
        history, deadline_ns=124 * HOUR_NS + 1, config=config, as_of_ns=100 * HOUR_NS
    ).can_become_ready


def test_availability_is_not_readiness_and_can_age_out() -> None:
    history = rows()
    history = tuple(
        replace(row, available_ns=200 * HOUR_NS) if i == 6 else row for i, row in enumerate(history)
    )
    result = assess_btc_readiness(
        history,
        config=engineering_canary(),
        as_of_ns=100 * HOUR_NS,
        deadline_ns=124 * HOUR_NS,
    )
    assert not result.ready
    assert result.reason == "INSUFFICIENT_HISTORY"
    assert result.earliest_possible_ready_ns == 102 * HOUR_NS
    assert result.can_become_ready


def test_short_history_can_recover_without_fabricating_features() -> None:
    history = rows()[-72:]
    result = assess_btc_readiness(
        history,
        config=engineering_canary(),
        as_of_ns=100 * HOUR_NS,
        deadline_ns=102 * HOUR_NS,
    )
    assert result.reason == "INSUFFICIENT_HISTORY"
    assert not result.ready
    assert result.can_become_ready
    assert result.earliest_possible_ready_ns == 101 * HOUR_NS
    assert engineering_canary().features(history, 100 * HOUR_NS) is None
