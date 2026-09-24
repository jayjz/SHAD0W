"""One unvalidated long/cash BTC trend candidate; no risk or execution authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from decimal import Decimal, localcontext
from enum import StrEnum

from shadow.domain.market import Instrument
from shadow.features.btc_trend import (
    BTC,
    BTC_CONTEXT,
    HOUR_NS,
    BtcTrendFeatures,
    CompletedBtcInterval,
    trend_features,
)


@dataclass(frozen=True, slots=True)
class BtcTrendConfig:
    instrument: Instrument
    interval_ns: int
    slow_horizon_ns: int
    fast_horizon_ns: int
    volatility_horizon_ns: int
    maximum_volatility: Decimal
    trailing_volatility_multiple: Decimal
    estimated_maker_fee: Decimal
    estimated_taker_fee: Decimal
    spread_allowance: Decimal
    slippage_allowance: Decimal
    cost_safety_margin: Decimal
    minimum_history: int
    maximum_evidence_age_ns: int
    strategy_version: str = "shadow.btc-trend.v1"

    def __post_init__(self) -> None:
        if self.instrument != BTC or self.strategy_version != "shadow.btc-trend.v1":
            raise ValueError("unsupported strategy identity")
        for value in (
            self.interval_ns,
            self.slow_horizon_ns,
            self.fast_horizon_ns,
            self.volatility_horizon_ns,
            self.minimum_history,
            self.maximum_evidence_age_ns,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("positive integer durations/history required")
        if any(
            h % self.interval_ns
            for h in (self.slow_horizon_ns, self.fast_horizon_ns, self.volatility_horizon_ns)
        ):
            raise ValueError("horizons must contain whole intervals")
        if self.fast_horizon_ns >= self.slow_horizon_ns:
            raise ValueError("fast horizon must be shorter than slow horizon")
        for amount in (
            self.maximum_volatility,
            self.trailing_volatility_multiple,
            self.estimated_maker_fee,
            self.estimated_taker_fee,
            self.spread_allowance,
            self.slippage_allowance,
            self.cost_safety_margin,
        ):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise ValueError("finite nonnegative Decimal parameters required")
        if self.maximum_volatility <= 0 or self.trailing_volatility_multiple <= 0:
            raise ValueError("positive volatility bounds required")

    @property
    def configuration_id(self) -> str:
        values = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Decimal):
                # Exact canonicalization without context-dependent normalize().
                text = format(value, "f")
                value = text.rstrip("0").rstrip(".") if "." in text else text
                if value == "-0":
                    value = "0"
            elif isinstance(value, Instrument):
                value = value.identifier
            values[field.name] = value
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    @property
    def round_trip_cost(self) -> Decimal:
        # MARKET entries and exits both assume taker fees. Maker assumption is
        # recorded for sealed comparisons, never substituted to obtain a trade.
        with localcontext(BTC_CONTEXT):
            return 2 * self.estimated_taker_fee + self.spread_allowance + self.slippage_allowance

    def features(
        self, intervals: tuple[CompletedBtcInterval, ...], now_ns: int
    ) -> BtcTrendFeatures | None:
        return trend_features(
            intervals,
            as_of_ns=now_ns,
            interval_ns=self.interval_ns,
            slow_periods=self.slow_horizon_ns // self.interval_ns,
            fast_periods=self.fast_horizon_ns // self.interval_ns,
            volatility_periods=self.volatility_horizon_ns // self.interval_ns,
            minimum_history=self.minimum_history,
        )


def engineering_canary() -> BtcTrendConfig:
    """Engineering fixture, not optimized or empirically validated parameters."""
    return BtcTrendConfig(
        BTC,
        HOUR_NS,
        72 * HOUR_NS,
        6 * HOUR_NS,
        24 * HOUR_NS,
        Decimal("0.03"),
        Decimal("3"),
        Decimal("0.0015"),
        Decimal("0.0025"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("0.002"),
        73,
        HOUR_NS + 60_000_000_000,
    )


class BtcAction(StrEnum):
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class BtcProposal:
    action: BtcAction
    reason: str
    configuration_id: str
    features: BtcTrendFeatures
    high_water_mark: Decimal | None
    round_trip_cost: Decimal
    safety_margin: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.action, BtcAction) or not isinstance(
            self.features, BtcTrendFeatures
        ):
            raise ValueError("typed BTC proposal required")
        if not self.reason or len(self.configuration_id) != 64:
            raise ValueError("proposal identity/reason required")
        for amount in (self.round_trip_cost, self.safety_margin):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise ValueError("finite nonnegative cost evidence required")
        if self.high_water_mark is not None and (
            not isinstance(self.high_water_mark, Decimal)
            or not self.high_water_mark.is_finite()
            or self.high_water_mark <= 0
        ):
            raise ValueError("invalid high water mark")


def high_water_since_entry(
    intervals: tuple[CompletedBtcInterval, ...],
    *,
    entry_ns: int,
    as_of_ns: int,
    interval_ns: int,
) -> Decimal | None:
    """Reconstruct from persisted completed closes, never a memory-only maximum.

    Requires every interval ending after entry through the current completed
    interval. A missing first close or interior gap invalidates exit state.
    """
    rows = tuple(row for row in intervals if entry_ns < row.end_ns <= as_of_ns)
    if not rows or not rows[0].start_ns <= entry_ns < rows[0].end_ns:
        return None
    if any(
        row.close is None or row.available_ns > as_of_ns or row.end_ns - row.start_ns != interval_ns
        for row in rows
    ):
        return None
    if any(a.end_ns != b.start_ns for a, b in zip(rows, rows[1:], strict=False)):
        return None
    return max(row.close for row in rows if row.close is not None)


def propose(
    config: BtcTrendConfig,
    features: BtcTrendFeatures | None,
    *,
    now_ns: int,
    holding: bool,
    high_water_mark: Decimal | None = None,
    independent_risk_halt: bool = False,
) -> BtcProposal | None:
    if features is None or not (
        features.end_ns <= features.available_ns <= now_ns
        and now_ns - features.end_ns <= config.maximum_evidence_age_ns
    ):
        return None
    with localcontext(BTC_CONTEXT):
        if holding:
            if (
                high_water_mark is None
                or not high_water_mark.is_finite()
                or high_water_mark < features.close
            ):
                return None
            threshold = high_water_mark * (
                1 - config.trailing_volatility_multiple * features.volatility
            )
            reason = (
                "independent_risk_halt"
                if independent_risk_halt
                else "slow_trend_reversal"
                if features.trend_distance <= 0
                else "trailing_volatility_stop"
                if features.close < threshold
                else None
            )
            action = BtcAction.EXIT
        else:
            reason = (
                "trend_confirmed"
                if (
                    not independent_risk_halt
                    and features.trend_distance > 0
                    and features.fast_return > 0
                    and features.volatility <= config.maximum_volatility
                    and features.trend_distance > config.round_trip_cost + config.cost_safety_margin
                )
                else None
            )
            action = BtcAction.ENTER
        if reason is None:
            return None
        return BtcProposal(
            action,
            reason,
            config.configuration_id,
            features,
            high_water_mark,
            config.round_trip_cost,
            config.cost_safety_margin,
        )
