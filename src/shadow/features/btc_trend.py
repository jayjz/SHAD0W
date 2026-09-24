"""Causal BTC trade intervals and return features, without operational authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from shadow.domain.crypto_market import CryptoTrade, UtcNanoseconds
from shadow.domain.market import Instrument

HOUR_NS = 3_600_000_000_000
BTC = Instrument("BTC/USD")
# Fixed precision/rounding makes caller Decimal context irrelevant.
BTC_CONTEXT = Context(prec=34)


@dataclass(frozen=True, slots=True)
class CompletedBtcInterval:
    start_ns: int
    end_ns: int
    available_ns: int
    close: Decimal | None
    closing_trade_id: str | None

    def __post_init__(self) -> None:
        for value in (self.start_ns, self.end_ns, self.available_ns):
            UtcNanoseconds(value)
        if not self.start_ns < self.end_ns <= self.available_ns:
            raise ValueError("invalid completed interval times")
        if (self.close is None) != (self.closing_trade_id is None):
            raise ValueError("gap must have neither close nor trade identity")
        if self.close is not None and (
            not isinstance(self.close, Decimal) or not self.close.is_finite() or self.close <= 0
        ):
            raise ValueError("positive Decimal close required")


class BtcIntervals:
    """Receive-ordered trades close [start,end) only upon a later trade.

    A capture beginning inside an interval discards that partial interval. Missing
    intervals are explicit None closes. Late/conflicting trades fail closed;
    completed intervals are never revised. Disconnects require a new builder and
    therefore a new contiguous warm-up. Replay must preserve capture order.
    """

    def __init__(self, *, started_ns: int, interval_ns: int = HOUR_NS) -> None:
        UtcNanoseconds(started_ns)
        if type(interval_ns) is not int or interval_ns <= 0:
            raise ValueError("positive integer interval required")
        self.interval_ns = interval_ns
        self._start = ((started_ns + interval_ns - 1) // interval_ns) * interval_ns
        self._started = started_ns
        self._last: CryptoTrade | None = None
        self._close: CryptoTrade | None = None

    def accept(self, trade: CryptoTrade) -> tuple[CompletedBtcInterval, ...]:
        if trade.instrument != BTC:
            raise ValueError("BTC/USD trades required")
        if trade.availability_time.value < self._started:
            raise ValueError("trade predates capture")
        if self._last is not None:
            if trade == self._last:
                return ()
            if (
                trade.observation_time < self._last.observation_time
                or trade.availability_time < self._last.availability_time
                or trade.trade_id == self._last.trade_id
            ):
                raise ValueError("late or conflicting trade evidence")
        self._last = trade
        observed = trade.observation_time.value
        if observed < self._start:
            # Only the initial partial interval may be ignored.
            if observed >= self._started and self._close is None:
                return ()
            raise ValueError("trade targets a completed interval")
        result = []
        while observed >= self._start + self.interval_ns:
            result.append(
                CompletedBtcInterval(
                    self._start,
                    self._start + self.interval_ns,
                    trade.availability_time.value,
                    None if self._close is None else self._close.price,
                    None if self._close is None else self._close.trade_id,
                )
            )
            self._start += self.interval_ns
            self._close = None
        self._close = trade
        return tuple(result)


@dataclass(frozen=True, slots=True)
class BtcTrendFeatures:
    end_ns: int
    available_ns: int
    close: Decimal
    baseline: Decimal
    trend_distance: Decimal
    fast_return: Decimal
    volatility: Decimal

    def __post_init__(self) -> None:
        UtcNanoseconds(self.end_ns)
        UtcNanoseconds(self.available_ns)
        if self.end_ns > self.available_ns:
            raise ValueError("feature availability precedes interval end")
        for value in (
            self.close,
            self.baseline,
            self.trend_distance,
            self.fast_return,
            self.volatility,
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("finite Decimal feature values required")
        if self.close <= 0 or self.baseline <= 0 or self.volatility < 0:
            raise ValueError("invalid price or return volatility")


def trend_features(
    intervals: tuple[CompletedBtcInterval, ...],
    *,
    as_of_ns: int,
    interval_ns: int,
    slow_periods: int,
    fast_periods: int,
    volatility_periods: int,
    minimum_history: int,
) -> BtcTrendFeatures | None:
    """Population stddev of simple interval returns; never annualized."""
    UtcNanoseconds(as_of_ns)
    if any(
        type(n) is not int or n <= 0
        for n in (interval_ns, slow_periods, fast_periods, volatility_periods, minimum_history)
    ):
        raise ValueError("positive integer windows required")
    count = max(slow_periods, fast_periods + 1, volatility_periods + 1, minimum_history)
    if len(intervals) < count:
        return None
    window = intervals[-count:]
    if any(
        row.end_ns > as_of_ns
        or row.available_ns > as_of_ns
        or row.close is None
        or row.end_ns - row.start_ns != interval_ns
        for row in window
    ) or any(a.end_ns != b.start_ns for a, b in zip(window, window[1:], strict=False)):
        return None
    prices = tuple(row.close for row in window if row.close is not None)
    with localcontext(BTC_CONTEXT):
        baseline = sum(prices[-slow_periods:], Decimal(0)) / slow_periods
        returns = tuple(b / a - 1 for a, b in zip(prices, prices[1:], strict=False))[
            -volatility_periods:
        ]
        mean = sum(returns, Decimal(0)) / volatility_periods
        variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / volatility_periods
        return BtcTrendFeatures(
            window[-1].end_ns,
            max(row.available_ns for row in window),
            prices[-1],
            baseline,
            prices[-1] / baseline - 1,
            prices[-1] / prices[-fast_periods - 1] - 1,
            variance.sqrt(),
        )
