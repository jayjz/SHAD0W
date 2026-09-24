from dataclasses import fields, replace
from decimal import Decimal, localcontext
from typing import Any

import pytest

from shadow.domain.crypto_market import CryptoTrade, TakerSide, UtcNanoseconds
from shadow.domain.market import AvailabilitySemantics, Provenance
from shadow.features.btc_trend import BTC, HOUR_NS, BtcIntervals, CompletedBtcInterval
from shadow.strategies.btc_trend import (
    BtcAction,
    engineering_canary,
    high_water_since_entry,
    propose,
)


def trade(hour: int, price: str = "100", *, delay: int = 1) -> CryptoTrade:
    return CryptoTrade(
        BTC,
        Decimal(price),
        Decimal("0.01"),
        str(hour),
        TakerSide.BUY,
        UtcNanoseconds(hour * HOUR_NS),
        UtcNanoseconds(hour * HOUR_NS + delay),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("synthetic"),
    )


def rising() -> tuple[CompletedBtcInterval, ...]:
    builder = BtcIntervals(started_ns=0)
    return tuple(row for hour in range(74) for row in builder.accept(trade(hour, str(100 + hour))))


def test_closed_intervals_and_integer_availability() -> None:
    builder = BtcIntervals(started_ns=0)
    assert builder.accept(trade(0)) == ()
    (row,) = builder.accept(trade(1, "110", delay=123))
    assert row.close == Decimal(100)  # next interval price cannot leak backwards
    assert row.end_ns == HOUR_NS
    assert row.available_ns == HOUR_NS + 123
    assert builder.accept(trade(1, "110", delay=123)) == ()
    with pytest.raises(ValueError, match="late"):
        builder.accept(trade(0))


def test_initial_partial_is_discarded_and_gaps_explicit() -> None:
    builder = BtcIntervals(started_ns=1)
    assert (
        builder.accept(
            replace(
                trade(0), observation_time=UtcNanoseconds(2), availability_time=UtcNanoseconds(3)
            )
        )
        == ()
    )
    rows = builder.accept(trade(3))
    assert [r.start_ns for r in rows] == [HOUR_NS, 2 * HOUR_NS]
    assert all(r.close is None for r in rows)


def test_causal_features_replay_and_context_independence() -> None:
    config = engineering_canary()
    rows = rising()
    assert rows == rising()
    now = rows[-1].available_ns
    feature = config.features(rows, now)
    assert feature is not None
    assert feature.close == 172
    assert feature.baseline == Decimal("136.5")
    assert feature.fast_return > 0 and feature.trend_distance > 0
    assert config.features(rows, now - 1) is None
    assert config.features(rows[:-1], now) is None
    assert config.features(rows[:40] + rows[41:], now) is None
    with localcontext() as context:
        context.prec = 5
        assert config.features(rows, now) == feature
        assert config.configuration_id == engineering_canary().configuration_id
    assert propose(config, feature, now_ns=now, holding=False).action is BtcAction.ENTER  # type: ignore[union-attr]


def test_entry_filters() -> None:
    config = engineering_canary()
    rows = rising()
    now = rows[-1].available_ns
    feature = config.features(rows, now)
    assert feature is not None
    for changed in (
        replace(feature, trend_distance=Decimal("-0.01")),
        replace(feature, fast_return=Decimal(0)),
        replace(feature, volatility=Decimal("0.031")),
        replace(feature, trend_distance=config.round_trip_cost + config.cost_safety_margin),
    ):
        assert propose(config, changed, now_ns=now, holding=False) is None
    assert (
        propose(config, feature, now_ns=now + config.maximum_evidence_age_ns, holding=False) is None
    )


def test_exits_and_dimensionally_explicit_stop() -> None:
    config = engineering_canary()
    rows = rising()
    now = rows[-1].available_ns
    feature = config.features(rows, now)
    assert feature is not None
    # 200 USD * (1 - 3 * 0.01 hourly return volatility) = 194 USD.
    feature = replace(feature, close=Decimal(193), volatility=Decimal("0.01"))
    exit_signal = propose(config, feature, now_ns=now, holding=True, high_water_mark=Decimal(200))
    assert exit_signal is not None and exit_signal.reason == "trailing_volatility_stop"
    assert (
        propose(
            config,
            replace(feature, close=Decimal(194)),
            now_ns=now,
            holding=True,
            high_water_mark=Decimal(200),
        )
        is None
    )
    assert propose(config, feature, now_ns=now, holding=True) is None
    reversal = propose(
        config,
        replace(feature, trend_distance=Decimal("-0.01")),
        now_ns=now,
        holding=True,
        high_water_mark=Decimal(200),
    )
    assert reversal is not None and reversal.reason == "slow_trend_reversal"
    halted = propose(
        config,
        feature,
        now_ns=now,
        holding=True,
        high_water_mark=Decimal(200),
        independent_risk_halt=True,
    )
    assert halted is not None and halted.reason == "independent_risk_halt"


def test_high_water_reconstruction_requires_contiguous_durable_closes() -> None:
    rows = rising()
    kwargs = dict(entry_ns=HOUR_NS + 3, as_of_ns=rows[-1].available_ns, interval_ns=HOUR_NS)
    assert high_water_since_entry(rows, **kwargs) == Decimal(172)
    assert high_water_since_entry(tuple(rows), **kwargs) == Decimal(172)
    assert high_water_since_entry(rows[2:], **kwargs) is None
    assert high_water_since_entry(rows[:40] + rows[41:], **kwargs) is None


def test_all_economic_parameters_bind_identity_and_cost() -> None:
    config = engineering_canary()
    assert config.round_trip_cost == Decimal("0.007")
    for field in fields(config):
        value = getattr(config, field.name)
        if isinstance(value, Decimal):
            changed: dict[str, Any] = {field.name: value + Decimal("0.0001")}
            assert replace(config, **changed).configuration_id != config.configuration_id
    assert replace(config, slow_horizon_ns=96 * HOUR_NS).configuration_id != config.configuration_id
    assert (
        replace(config, estimated_taker_fee=Decimal("0.002500")).configuration_id
        == config.configuration_id
    )
