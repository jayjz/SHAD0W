"""Offline regressions for the bounded P4A Alpaca live-data shadow path."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.adapters.alpaca.normalize import translate
from shadow.adapters.alpaca.stream import (
    DataCredentials,
    _local_feed_url_checked,
    _subscription_is_exact,
    consume,
    consume_local,
    decode_frame,
    run_live,
)
from shadow.application import live_shadow
from shadow.application.evidence import (
    CaptureError,
    CaptureStatus,
    EvidenceWriter,
    bootstrap_from_capture,
    line,
    load_capture,
    replay,
    verify_capture,
)
from shadow.application.shadow import (
    BootstrapEvidence,
    FeedHealth,
    RiskObservabilityStatus,
    ShadowConfig,
    ShadowRecord,
    ShadowSession,
)
from shadow.domain import Bar, Instrument, Quote
from shadow.execution.opportunity import source_opportunity_key_for_intent
from shadow.risk import OperationalQuantityConfig, OperatorControls, RiskPolicy, RiskState
from shadow.strategies import MeanReversionConfig

UTC_TIME = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def at(minutes: int, seconds: int = 0) -> datetime:
    return UTC_TIME + timedelta(minutes=minutes, seconds=seconds)


def bar(
    minute: int, close: str, *, symbol: str = "AAPL", received_delay: int = 1
) -> dict[str, object]:
    return {
        "T": "b",
        "S": symbol,
        "o": Decimal("100"),
        "h": Decimal("101"),
        "l": Decimal("90"),
        "c": Decimal(close),
        "v": Decimal("12"),
        "t": at(minute).isoformat().replace("+00:00", "Z"),
        "received": at(minute + 1, received_delay),
    }


def quote(
    minute: int, *, symbol: str = "AAPL", bid: str = "99", ask: str = "100"
) -> dict[str, object]:
    return {
        "T": "q",
        "S": symbol,
        "bp": Decimal(bid),
        "ap": Decimal(ask),
        "bs": Decimal("4"),
        "as": Decimal("5"),
        "t": at(minute, 30).isoformat().replace("+00:00", "Z"),
        "received": at(minute, 31),
    }


def observation(payload: dict[str, object]) -> Bar | Quote:
    received = payload.pop("received")
    assert isinstance(received, datetime)
    return translate(payload, received_at=received, symbols=("AAPL", "MSFT"), feed="iex")


def config(*, symbols: tuple[str, ...] = ("AAPL",), state: RiskState | None = None) -> ShadowConfig:
    instruments = tuple(Instrument(symbol) for symbol in symbols)
    strategies = tuple(
        MeanReversionConfig(
            instrument, f"{symbol}-strategy", 3, Decimal("-1"), Decimal("0"), timedelta(minutes=5)
        )
        for symbol, instrument in zip(symbols, instruments, strict=True)
    )
    quantities = tuple(
        OperationalQuantityConfig(instrument, f"{symbol}-quantity", Decimal("1"))
        for symbol, instrument in zip(symbols, instruments, strict=True)
    )
    return ShadowConfig(
        "shadow-test",
        "shadow-test-scope",
        "test-revision",
        strategies,
        quantities,
        RiskPolicy(
            "test-policy",
            True,
            instruments,
            Decimal("1"),
            2,
            timedelta(minutes=5),
            timedelta(minutes=5),
            timedelta(minutes=1),
            timedelta(minutes=1),
        ),
        "alpaca:iex",
        timedelta(minutes=5),
        timedelta(minutes=1),
        state,
    )


def session(
    *, symbols: tuple[str, ...] = ("AAPL",), state: RiskState | None = None
) -> ShadowSession:
    result = ShadowSession(config(symbols=symbols, state=state))
    result.control("connected", at(0))
    return result


def test_bar_start_translation_and_receive_availability() -> None:
    translated = observation(bar(0, "99"))
    assert translated.observation_time == at(1)
    assert translated.availability_time == at(1, 1)
    assert translated.availability_time >= translated.observation_time


def test_quote_translation_preserves_sizes_and_locked_crossed_states() -> None:
    locked = observation(quote(0, bid="100", ask="100"))
    crossed = observation(quote(1, bid="101", ask="100"))
    assert isinstance(locked, Quote) and isinstance(crossed, Quote)
    assert locked.bid_size == Decimal("4") and locked.ask_size == Decimal("5")
    assert locked.market_state.value == "locked"
    assert crossed.market_state.value == "crossed"


@pytest.mark.parametrize(
    "payload",
    [
        {"T": "q", "S": "AAPL", "bp": "99", "ap": Decimal("100"), "t": "2025-01-02T14:30:00Z"},
        {
            "T": "q",
            "S": "AAPL",
            "bp": Decimal("99"),
            "ap": Decimal("100"),
            "t": "2025-01-02T14:30:00",
        },
    ],
)
def test_malformed_numeric_or_naive_timestamp_reject(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        translate(payload, received_at=at(1), symbols=("AAPL",), feed="iex")


def test_future_provider_time_and_unfinished_bar_reject() -> None:
    with pytest.raises(ValueError, match="after receipt"):
        translate(quote(2), received_at=at(1), symbols=("AAPL",), feed="iex")
    raw = bar(2, "99")
    with pytest.raises(ValueError, match="ends after receipt"):
        translate(raw, received_at=at(2, 30), symbols=("AAPL",), feed="iex")


def test_unsupported_symbol_rejects_explicitly() -> None:
    with pytest.raises(ValueError, match="outside configured scope"):
        translate(quote(0, symbol="MSFT"), received_at=at(1), symbols=("AAPL",), feed="iex")


def _three_bars(shadow: ShadowSession) -> list[ShadowRecord]:
    return [
        shadow.accept(observation(bar(index, close)), f"bar-{index}")
        for index, close in enumerate(("100", "101", "90"))
    ]


def _two_bars_then_quote_then_final_bar(shadow: ShadowSession) -> list[ShadowRecord]:
    records = [
        shadow.accept(observation(bar(index, close)), f"bar-{index}")
        for index, close in enumerate(("100", "101"))
    ]
    shadow.accept(observation(quote(2)), "quote-2")
    records.append(shadow.accept(observation(bar(2, "90")), "bar-2"))
    return records


def test_duplicate_bar_and_quote_are_idempotent() -> None:
    shadow = session()
    raw_quote = quote(0)
    first_quote = observation(raw_quote)
    shadow.accept(first_quote, "quote-0")
    _three_bars(shadow)
    duplicate = shadow.accept(observation(bar(2, "90")), "bar-2")
    duplicate_quote = shadow.accept(
        translate(raw_quote, received_at=at(3, 2), symbols=("AAPL", "MSFT"), feed="iex"), "quote-0"
    )
    assert duplicate.disposition == "duplicate" and duplicate.signal is None
    assert duplicate_quote.disposition == "duplicate"
    assert len([record for record in shadow.records if record.signal is not None]) == 1


def test_same_time_variant_and_delayed_observations_never_rewrite() -> None:
    shadow = session()
    shadow.accept(observation(bar(0, "100")), "bar-0")
    shadow.accept(observation(bar(1, "101")), "bar-1")
    current = observation(quote(2))
    shadow.accept(current, "quote-current")
    variant = observation(quote(2, bid="98"))
    same_time = shadow.accept(variant, "quote-variant")
    old_raw = quote(1)
    older = shadow.accept(
        translate(old_raw, received_at=at(2, 32), symbols=("AAPL", "MSFT"), feed="iex"), "quote-old"
    )
    assert same_time.disposition == "same_time_variant"
    assert older.disposition == "out_of_order"
    shadow.accept(observation(bar(2, "90")), "bar-2")
    assert shadow.records[-1].risk_observability is not None
    assert shadow.records[-1].risk_observability.quote == current


def test_delayed_bar_and_reconnect_redelivery_never_create_another_signal() -> None:
    shadow = session()
    shadow.accept(observation(quote(2)), "quote-2")
    # Deliver the bar sequence chronologically after the quote's receipt for this test.
    for index, close in enumerate(("100", "101", "90"), start=2):
        raw = bar(index, close)
        shadow.accept(
            translate(raw, received_at=at(index + 1, 1), symbols=("AAPL",), feed="iex"),
            f"bar-{index}",
        )
    shadow.control("disconnected", at(6), "test_reconnect")
    shadow.control("connected", at(6, 1))
    late_raw = bar(1, "100")
    delayed = shadow.accept(
        translate(late_raw, received_at=at(6, 2), symbols=("AAPL",), feed="iex"), "late-bar"
    )
    redelivery_raw = bar(4, "90")
    redelivery = shadow.accept(
        translate(redelivery_raw, received_at=at(6, 3), symbols=("AAPL",), feed="iex"), "bar-4"
    )
    assert delayed.disposition == "out_of_order"
    assert redelivery.disposition == "duplicate"
    assert len([record for record in shadow.records if record.signal is not None]) == 1


def test_bars_are_healthy_without_quotes_but_candidate_is_not_risk_ready() -> None:
    shadow = session()
    records = _three_bars(shadow)
    last = records[-1]
    assert shadow.health is FeedHealth.HEALTHY
    assert last.signal is not None
    assert last.risk_observability is not None
    assert last.risk_observability.status is RiskObservabilityStatus.QUOTE_NOT_READY


def test_stale_new_live_bar_cannot_drive_a_strategy_decision() -> None:
    shadow = session()
    shadow.accept(observation(bar(0, "100")), "bar")
    stale = shadow.accept(
        translate(bar(1, "90"), received_at=at(8), symbols=("AAPL",), feed="iex"), "stale-bar"
    )
    tick = shadow.control("tick", at(8))
    disconnected = shadow.control("disconnected", at(8, 1), "test")
    assert stale.disposition == "accepted"
    assert stale.feature is not None
    assert stale.signal is None
    assert stale.intent is None
    assert tick.health is FeedHealth.STALE
    assert disconnected.health is FeedHealth.DISCONNECTED


def test_symbols_are_isolated() -> None:
    shadow = session(symbols=("AAPL", "MSFT"))
    for index, close in enumerate(("100", "101", "90")):
        raw = bar(index, close, symbol="AAPL")
        translated = observation(raw)
        shadow.accept(translated)
    aapl_signals = [record.signal for record in shadow.records if record.signal is not None]
    assert len(aapl_signals) == 1
    assert aapl_signals[0].instrument == Instrument("AAPL")


def test_invalid_record_does_not_poison_later_valid_input() -> None:
    shadow = session()
    invalid = shadow.invalid(at(0, 1), "malformed_numeric")
    accepted = shadow.accept(observation(bar(0, "100")), "good")
    assert invalid.disposition.startswith("invalid:")
    assert accepted.disposition == "accepted"


def test_replay_retains_one_invalid_prefix_for_every_reason() -> None:
    shadow = session()
    first = shadow.invalid(at(0, 1), "malformed_frame", "bad-frame")
    second = shadow.invalid(at(0, 2), "unexpected_message", "bad-message")

    replayed = replay(shadow.config, (shadow.records[0], first, second))

    assert replayed[-2:].__eq__((first, second))


def test_replay_and_future_append_preserve_existing_records() -> None:
    shadow = session()
    shadow.accept(observation(quote(0)), "quote")
    _three_bars(shadow)
    captured = shadow.records
    assert replay(shadow.config, captured) == captured
    shadow.accept(observation(bar(3, "91")), "future")
    assert shadow.records[: len(captured)] == captured


def _write_capture(
    path: Path, shadow: ShadowSession, bootstrap: BootstrapEvidence | None = None
) -> None:
    writer = EvidenceWriter(path, shadow.config, bootstrap)
    try:
        for record in shadow.records:
            writer.write(record)
    finally:
        writer.close()


def _complete_capture(*, state: RiskState | None = None) -> ShadowSession:
    shadow = session(state=state)
    _two_bars_then_quote_then_final_bar(shadow)
    shadow.control("stopped", at(4), "test_complete")
    return shadow


def _warm_config(
    *, symbol: str = "AAPL", source_dataset_id: str = "alpaca:iex:aapl-1m-v1"
) -> ShadowConfig:
    base = config(symbols=(symbol,))
    strategy = replace(
        base.strategies[0],
        configuration_id=f"{symbol}-mean-reversion-v1",
        rolling_window=20,
    )
    return replace(
        base,
        session_id="warm-source",
        strategies=(strategy,),
        source_dataset_id=source_dataset_id,
    )


def _warm_source_capture(path: Path, source_config: ShadowConfig) -> ShadowSession:
    source = ShadowSession(source_config)
    source.control("connected", at(0))
    closes = ("99", "100", "101") * 6 + ("100",)
    assert len(closes) == 19
    symbol = source_config.strategies[0].instrument.identifier
    for index, close in enumerate(closes):
        source.accept(observation(bar(index, close, symbol=symbol)))
    source.control("stopped", at(20), "test_complete")
    _write_capture(path, source)
    return source


def _bootstrapped_session(path: Path, target_config: ShadowConfig) -> ShadowSession:
    seeded = ShadowSession(target_config)
    bootstrap = bootstrap_from_capture(path, target_config, now=at(19, 2))
    seeded.bootstrap(bootstrap, at(19, 2))
    seeded.control("connected", at(19, 2))
    return seeded


def test_live_shadow_main_persists_bootstrap_before_connected_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.jsonl"
    target_path = tmp_path / "target.jsonl"
    arguments = [
        "--session-id",
        "warm-target",
        "--operational-scope",
        "paper-primary",
        "--code-revision",
        "test-revision",
        "--symbol",
        "AAPL",
        "--evidence-path",
        str(target_path),
        "--bootstrap-capture",
        str(source_path),
        "--local-feed-url",
        "ws://127.0.0.1:8765",
        "--source-dataset-id",
        "alpaca:iex:aapl-1m-v1",
        "--strategy-configuration-id",
        "AAPL-mean-reversion-v1",
        "--quantity-configuration-id",
        "AAPL-quantity-v1",
    ]
    target_config = live_shadow._config(live_shadow._parser().parse_args(arguments))
    source = ShadowSession(replace(target_config, session_id="warm-source"))
    source.control("connected", at(0))
    for index, close in enumerate(("99", "100", "101") * 6 + ("100", "99")):
        source.accept(observation(bar(index, close)))
    source.control("stopped", at(21), "test_complete")
    _write_capture(source_path, source)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            assert tz is UTC
            return at(21, 1)

    bootstrap_calls = 0
    bootstrap_records: list[ShadowRecord] = []

    class CountingSession(ShadowSession):
        def bootstrap(self, evidence: BootstrapEvidence, now: datetime) -> ShadowRecord:
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            record = super().bootstrap(evidence, now)
            bootstrap_records.append(record)
            return record

    expected_records: tuple[ShadowRecord, ...] = ()

    async def run_once(
        live_session: ShadowSession,
        credentials: DataCredentials | None,
        writer: EvidenceWriter,
        *,
        duration: float,
        feed: str,
        local_feed_url: str | None = None,
    ) -> None:
        nonlocal expected_records
        assert credentials is None
        assert duration == 60.0
        assert feed == "iex"
        assert local_feed_url == "ws://127.0.0.1:8765"
        writer.write(live_session.control("connected", at(22), "local_relay"))
        writer.write(live_session.control("stopped", at(23), "test_complete"))
        expected_records = live_session.records

    monkeypatch.setattr(live_shadow, "datetime", FrozenDateTime)
    monkeypatch.setattr(live_shadow, "ShadowSession", CountingSession)
    monkeypatch.setattr(live_shadow, "run_live", run_once)
    monkeypatch.setattr(sys, "argv", ["live-shadow", *arguments])

    assert live_shadow.main() == 0
    assert bootstrap_calls == 1
    assert len(bootstrap_records) == 1
    lines = target_path.read_text(encoding="utf-8").splitlines()
    persisted = [json.loads(value) for value in lines]
    assert "bootstrap" in persisted[0]
    assert lines[1] == line(bootstrap_records[0])
    assert [(record["sequence"], record["action"]) for record in persisted[1:]] == [
        (0, "bootstrap"),
        (1, "connected"),
        (2, "stopped"),
    ]
    capture = load_capture(target_path)
    assert verify_capture(capture).records == expected_records


def test_cold_start_capture_has_no_bootstrap_record_and_starts_at_zero(tmp_path: Path) -> None:
    shadow = session()
    shadow.control("stopped", at(1), "test_complete")
    path = tmp_path / "cold.jsonl"
    _write_capture(path, shadow)

    persisted = [json.loads(value) for value in path.read_text(encoding="utf-8").splitlines()]
    assert "bootstrap" not in persisted[0]
    assert [(record["sequence"], record["action"]) for record in persisted[1:]] == [
        (0, "connected"),
        (1, "stopped"),
    ]


def test_capture_with_first_persisted_sequence_one_fails(tmp_path: Path) -> None:
    shadow = session()
    shadow.control("stopped", at(1), "test_complete")
    path = tmp_path / "sequence-gap.jsonl"
    _write_capture(path, shadow)
    lines = path.read_text(encoding="utf-8").splitlines()
    first_record = json.loads(lines[1])
    first_record["sequence"] = 1
    lines[1] = line(first_record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CaptureError, match="non-sequential record sequence"):
        load_capture(path)


def test_no_bootstrap_retains_twenty_bar_cold_start() -> None:
    cold = ShadowSession(_warm_config())
    cold.control("connected", at(0))
    closes = ("99", "100", "101") * 6 + ("100", "90")
    records = [cold.accept(observation(bar(index, close))) for index, close in enumerate(closes)]

    states = [record.feature.state.value for record in records[:19] if record.feature is not None]
    assert states == ["warming_up"] * 19
    assert records[19].feature is not None
    assert records[19].feature.state.value == "ready"


def test_verified_nineteen_bar_bootstrap_makes_first_live_bar_ready(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    target = replace(_warm_config(), session_id="warm-target")
    _warm_source_capture(source_path, target)

    seeded = _bootstrapped_session(source_path, target)
    live = seeded.accept(observation(bar(19, "90")), "live-19")

    assert seeded.records[0].action == "bootstrap"
    assert seeded.records[0].disposition == "history_seeded"
    assert live.feature is not None and live.feature.state.value == "ready"
    assert live.signal is not None
    assert isinstance(live.observation, Bar)
    assert live.signal.feature_observation_time == live.observation.observation_time


def test_bootstrap_alone_cannot_create_candidate_or_intent(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    target = replace(_warm_config(), session_id="warm-target")
    _warm_source_capture(source_path, target)

    seeded = ShadowSession(target)
    seeded.bootstrap(bootstrap_from_capture(source_path, target, now=at(19, 2)), at(19, 2))

    assert len(seeded.records) == 1
    assert seeded.records[0].feature is None
    assert seeded.records[0].signal is None
    assert seeded.records[0].intent is None


def test_bootstrap_rejects_incompatible_dataset_and_incomplete_capture(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    source_config = _warm_config()
    source = _warm_source_capture(source_path, source_config)
    incompatible = replace(source_config, source_dataset_id="alpaca:iex:other-1m-v1")
    with pytest.raises(CaptureError, match="lineage"):
        bootstrap_from_capture(source_path, incompatible, now=at(19, 2))
    with pytest.raises(CaptureError, match="operational scope"):
        bootstrap_from_capture(
            source_path,
            replace(source_config, operational_scope="paper-secondary"),
            now=at(19, 2),
        )

    incomplete_path = tmp_path / "incomplete.jsonl"
    incomplete = ShadowSession(source_config)
    incomplete.control("connected", at(0))
    for index, close in enumerate(("99", "100", "101") * 6 + ("100",)):
        incomplete.accept(observation(bar(index, close)))
    _write_capture(incomplete_path, incomplete)
    with pytest.raises(CaptureError, match="complete_stopped"):
        bootstrap_from_capture(incomplete_path, source_config, now=at(19, 2))

    assert source.health is FeedHealth.STOPPED


def test_bootstrap_accepts_historical_context_but_rejects_future_evidence_and_another_symbol(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "future.jsonl"
    source_config = _warm_config()
    _warm_source_capture(source_path, source_config)
    with pytest.raises(CaptureError, match="time boundary"):
        bootstrap_from_capture(source_path, source_config, now=at(5))
    historical = bootstrap_from_capture(source_path, source_config, now=at(30))
    assert historical.newest_observation_time == at(19)
    seeded = ShadowSession(source_config).bootstrap(historical, at(30))
    assert seeded.disposition == "history_seeded"

    other_path = tmp_path / "msft.jsonl"
    other = _warm_config(symbol="MSFT")
    _warm_source_capture(other_path, other)
    with pytest.raises(CaptureError, match="strategy configuration"):
        bootstrap_from_capture(other_path, source_config, now=at(19, 2))


def test_bootstrap_overlap_duplicate_is_idempotent_but_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.jsonl"
    target = _warm_config()
    _warm_source_capture(source_path, target)
    seeded = _bootstrapped_session(source_path, target)

    exact_raw = bar(18, "100")
    exact_raw["received"] = at(20)
    duplicate = seeded.accept(observation(exact_raw), "redelivery")
    assert duplicate.disposition == "duplicate"
    assert duplicate.feature is None

    conflicting_raw = bar(18, "90")
    conflicting_raw["received"] = at(20, 1)
    with pytest.raises(ValueError, match="conflicting live bar overlaps bootstrap"):
        seeded.accept(observation(conflicting_raw), "conflict")
    assert seeded.health is FeedHealth.FAILED


def test_bootstrap_evidence_replays_and_future_live_records_do_not_rewrite_it(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.jsonl"
    target_path = tmp_path / "target.jsonl"
    target = replace(_warm_config(), session_id="warm-target")
    _warm_source_capture(source_path, target)
    bootstrap = bootstrap_from_capture(source_path, target, now=at(19, 2))
    seeded = ShadowSession(target)
    seeded.bootstrap(bootstrap, at(19, 2))
    seeded.control("connected", at(19, 2))
    first_live = seeded.accept(observation(bar(19, "90")), "live-19")
    prefix = seeded.records
    seeded.accept(observation(bar(20, "91")), "live-20")
    seeded.control("stopped", at(22), "test_complete")
    _write_capture(target_path, seeded, bootstrap)

    capture = load_capture(target_path)
    assert capture.bootstrap == bootstrap
    assert verify_capture(capture).records == seeded.records
    assert seeded.records[: len(prefix)] == prefix
    assert first_live.feature is not None
    assert first_live.feature.observation_time == at(20)


def test_bootstrap_path_does_not_change_causal_strategy_identity(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    copied_path = tmp_path / "renamed-source.jsonl"
    target = _warm_config()
    _warm_source_capture(source_path, target)
    copied_path.write_bytes(source_path.read_bytes())
    first = bootstrap_from_capture(source_path, target, now=at(19, 2))
    second = bootstrap_from_capture(copied_path, target, now=at(19, 2))
    assert first.capture_digest == second.capture_digest

    left = ShadowSession(target)
    right = ShadowSession(target)
    left.bootstrap(first, at(19, 2))
    right.bootstrap(second, at(19, 2))
    left.control("connected", at(19, 2))
    right.control("connected", at(19, 2))
    live = observation(bar(19, "90"))
    assert left.accept(live, "live") == right.accept(live, "live")


def test_disk_capture_round_trip_recomputes_derived_evidence_and_is_terminal(
    tmp_path: Path,
) -> None:
    state = RiskState(
        "shadow-test-scope",
        "supplied-offline-state",
        1,
        True,
        (),
        (),
        at(3),
        at(3),
        OperatorControls(True, False, at(3), at(3)),
    )
    shadow = _complete_capture(state=state)
    path = tmp_path / "complete.jsonl"
    _write_capture(path, shadow)

    capture = load_capture(path)
    replayed = verify_capture(capture)

    assert capture.status is CaptureStatus.COMPLETE_STOPPED
    assert capture.config.operational_scope == "shadow-test-scope"
    assert replayed.records == shadow.records
    assert replayed.records[-2].feature is not None
    assert replayed.records[-2].signal is not None
    assert replayed.records[-2].risk_observability is not None
    with pytest.raises(ValueError, match="terminal shadow session"):
        replayed.accept(observation(bar(4, "92")))


def test_clean_incomplete_disk_prefix_replays_and_can_append(tmp_path: Path) -> None:
    shadow = session()
    shadow.accept(observation(bar(0, "100")), "bar-0")
    path = tmp_path / "incomplete.jsonl"
    _write_capture(path, shadow)

    capture = load_capture(path)
    replayed = verify_capture(capture)
    prefix = replayed.records
    replayed.accept(observation(bar(1, "101")), "bar-1")

    assert capture.status is CaptureStatus.INCOMPLETE
    assert capture.diagnostic == "capture ended without terminal evidence"
    assert replayed.records[: len(prefix)] == prefix


def test_truncated_final_line_recovers_verified_prefix_as_incomplete(tmp_path: Path) -> None:
    shadow = session()
    shadow.accept(observation(quote(0)), "quote")
    path = tmp_path / "truncated.jsonl"
    _write_capture(path, shadow)
    with path.open("a", encoding="utf-8") as evidence:
        evidence.write('{"sequence":2')

    capture = load_capture(path)

    assert capture.status is CaptureStatus.INCOMPLETE
    assert capture.diagnostic == "truncated final JSON line"
    assert verify_capture(capture).records == shadow.records


def test_truncation_after_terminal_record_is_never_claimed_complete(tmp_path: Path) -> None:
    shadow = _complete_capture()
    path = tmp_path / "terminal-truncated.jsonl"
    _write_capture(path, shadow)
    with path.open("a", encoding="utf-8") as evidence:
        evidence.write('{"interrupted"')

    capture = load_capture(path)

    assert capture.status is CaptureStatus.INCOMPLETE
    assert capture.diagnostic == "truncated final JSON line"
    replayed = verify_capture(capture)
    assert replayed.records == shadow.records
    with pytest.raises(ValueError, match="terminal shadow session"):
        replayed.accept(observation(bar(4, "92")))


def test_malformed_complete_interior_line_is_invalid(tmp_path: Path) -> None:
    shadow = _complete_capture()
    path = tmp_path / "corrupt.jsonl"
    _write_capture(path, shadow)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join((lines[0], "{not-json", *lines[1:])) + "\n", encoding="utf-8")

    with pytest.raises(CaptureError, match="malformed complete record"):
        load_capture(path)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.__setitem__("schema", "shadow.live.v999"), "unsupported capture schema"),
        (lambda item: item.__setitem__("config", {}), "invalid capture config shape"),
        (
            lambda item: item["config"].pop("maximum_bar_age"),
            "invalid capture config shape",
        ),
        (
            lambda item: item["config"].pop("operational_scope"),
            "invalid capture config shape",
        ),
        (
            lambda item: item["config"].__setitem__("maximum_quote_age", "not-a-duration"),
            "invalid capture config",
        ),
        (
            lambda item: item["config"]["strategies"][0].__setitem__("feature_name", "unknown"),
            "invalid capture config",
        ),
    ],
)
def test_capture_header_validation(tmp_path: Path, mutate: object, match: str) -> None:
    shadow = _complete_capture()
    path = tmp_path / "bad-header.jsonl"
    _write_capture(path, shadow)
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert callable(mutate)
    mutate(header)
    path.write_text("\n".join((line(header), *lines[1:])) + "\n", encoding="utf-8")

    with pytest.raises(CaptureError, match=match):
        load_capture(path)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.__setitem__("sequence", 3), "non-sequential"),
        (lambda item: item.__setitem__("session_id", "other-session"), "differs from config"),
        (lambda item: item.__setitem__("health", "unknown"), "invalid record health"),
        (lambda item: item.__setitem__("action", "unknown"), "invalid record action"),
        (lambda item: item["observation"].__setitem__("close", "not-a-decimal"), "invalid bar"),
        (lambda item: item["observation"].__setitem__("close", "NaN"), "invalid bar"),
        (
            lambda item: item["observation"].__setitem__("availability_semantics", "unknown"),
            "invalid bar",
        ),
        (
            lambda item: item["observation"].__setitem__(
                "availability_time", "2025-01-02T14:31:01"
            ),
            "invalid bar",
        ),
    ],
)
def test_capture_input_validation(tmp_path: Path, mutate: object, match: str) -> None:
    shadow = _complete_capture()
    path = tmp_path / "bad-record.jsonl"
    _write_capture(path, shadow)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    assert callable(mutate)
    mutate(record)
    lines[2] = line(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CaptureError, match=match):
        load_capture(path)


def test_invalid_record_disposition_is_rejected_from_disk(tmp_path: Path) -> None:
    shadow = session()
    shadow.invalid(at(0, 1), "malformed_frame", "bad-frame")
    shadow.control("stopped", at(1), "test_complete")
    path = tmp_path / "invalid-disposition.jsonl"
    _write_capture(path, shadow)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    record["disposition"] = "malformed_frame"
    lines[2] = line(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CaptureError, match="invalid observation disposition"):
        load_capture(path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("feature", {"value": "999"}),
        ("signal", {"reason": "exit_threshold"}),
        ("health", "stale"),
        ("observation", {"invalid": "observation"}),
    ],
)
def test_verification_rejects_tampered_derived_or_normalized_evidence(
    tmp_path: Path, field: str, replacement: object
) -> None:
    shadow = _complete_capture()
    path = tmp_path / "tampered.jsonl"
    _write_capture(path, shadow)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-2])
    record[field] = replacement
    lines[-2] = line(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if field == "observation":
        with pytest.raises(CaptureError, match="invalid quote shape"):
            load_capture(path)
        return
    capture = load_capture(path)
    with pytest.raises(CaptureError, match="differs"):
        verify_capture(capture)


def test_disk_replay_preserves_controls_and_nonforward_observations(tmp_path: Path) -> None:
    shadow = session()
    shadow.accept(observation(quote(0)), "quote-0")
    shadow.accept(observation(quote(0, bid="98")), "quote-variant")
    shadow.accept(observation(bar(1, "100")), "bar-1")
    shadow.accept(observation(bar(1, "100")), "bar-1")
    delayed_raw = bar(0, "100")
    shadow.accept(
        translate(delayed_raw, received_at=at(2, 32), symbols=("AAPL",), feed="iex"),
        "bar-old",
    )
    shadow.invalid(at(2, 33), "unexpected_message", "bad-message")
    shadow.control("tick", at(7), "receive_timeout")
    shadow.control("disconnected", at(7, 1), "test_disconnect")
    shadow.control("connected", at(7, 2), "test_reconnect")
    shadow.control("failed", at(7, 3), "test_complete")
    path = tmp_path / "all-dispositions.jsonl"
    _write_capture(path, shadow)

    capture = load_capture(path)
    replayed = verify_capture(capture)

    assert capture.status is CaptureStatus.COMPLETE_FAILED
    assert replayed.records == shadow.records
    assert {record.disposition for record in replayed.records} >= {
        "duplicate",
        "same_time_variant",
        "out_of_order",
        "invalid:unexpected_message",
    }


class _FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def recv(self) -> str:
        if not self.frames:
            raise OSError("test transport ended")
        return self.frames.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(message)


class _FakeConnection:
    def __init__(self, socket: _FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _FakeSocket:
        return self.socket

    async def __aexit__(self, *args: object) -> None:
        return None


def _subscription_acknowledgement(**channels: object) -> list[dict[str, object]]:
    return decode_frame(line([{"T": "subscription", **channels}]))


def test_subscription_acknowledgement_accepts_real_sparse_alpaca_frame() -> None:
    acknowledgement = _subscription_acknowledgement(quotes=["AAPL"], bars=["AAPL"])

    assert _subscription_is_exact(acknowledgement, ("AAPL",))


def test_subscription_acknowledgement_accepts_explicit_empty_optional_channel() -> None:
    acknowledgement = _subscription_acknowledgement(quotes=["AAPL"], bars=["AAPL"], trades=[])

    assert _subscription_is_exact(acknowledgement, ("AAPL",))


@pytest.mark.parametrize(
    "channels",
    [
        {"quotes": ["AAPL"]},
        {"bars": ["AAPL"]},
        {"quotes": ["AAPL"], "bars": ["MSFT"]},
        {"quotes": ["MSFT"], "bars": ["AAPL"]},
        {"quotes": ["AAPL"], "bars": ["AAPL"], "trades": ["AAPL"]},
    ],
)
def test_subscription_acknowledgement_rejects_missing_or_unexpected_scope(
    channels: dict[str, object],
) -> None:
    acknowledgement = _subscription_acknowledgement(**channels)

    assert not _subscription_is_exact(acknowledgement, ("AAPL",))


@pytest.mark.parametrize("trades", [None, "AAPL", {"symbol": "AAPL"}, ["AAPL", 1]])
def test_subscription_acknowledgement_rejects_malformed_present_optional_channel(
    trades: object,
) -> None:
    acknowledgement = _subscription_acknowledgement(quotes=["AAPL"], bars=["AAPL"], trades=trades)

    with pytest.raises(ValueError, match="invalid provider subscription acknowledgement"):
        _subscription_is_exact(acknowledgement, ("AAPL",))


def test_subscription_acknowledgement_rejects_duplicate_symbols() -> None:
    acknowledgement = _subscription_acknowledgement(quotes=["AAPL", "AAPL"], bars=["AAPL"])

    with pytest.raises(ValueError, match="duplicate provider subscription acknowledgement"):
        _subscription_is_exact(acknowledgement, ("AAPL",))


def test_subscription_acknowledgement_accepts_multiple_symbols_in_any_order() -> None:
    acknowledgement = _subscription_acknowledgement(quotes=["MSFT", "AAPL"], bars=["AAPL", "MSFT"])

    assert _subscription_is_exact(acknowledgement, ("AAPL", "MSFT"))


@pytest.mark.parametrize(
    ("frames", "reason"),
    [
        ([line([{"T": "success", "msg": "authenticated"}])], "connection_rejected"),
        (
            [
                line([{"T": "success", "msg": "connected"}]),
                line([{"T": "error", "msg": "unauthorized"}]),
            ],
            "authentication_rejected",
        ),
        (
            [line([{"T": "error", "code": 406, "msg": "connection limit exceeded"}])],
            "authentication_connection_limit",
        ),
        (
            [
                line([{"T": "success", "msg": "connected"}]),
                line([{"T": "success", "msg": "authenticated"}]),
                line([{"T": "subscription", "quotes": ["AAPL"], "bars": ["MSFT"]}]),
            ],
            "subscription_mismatch",
        ),
    ],
)
def test_run_live_records_sanitized_setup_failure_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[str],
    reason: str,
) -> None:
    socket = _FakeSocket(frames)
    monkeypatch.setattr(
        "websockets.asyncio.client.connect", lambda *args, **kwargs: _FakeConnection(socket)
    )
    shadow = session()
    writer = EvidenceWriter(tmp_path / "setup-failure.jsonl", shadow.config)

    try:
        with pytest.raises(ValueError, match="sanitized evidence"):
            asyncio.run(
                run_live(
                    shadow,
                    DataCredentials("key", "secret"),
                    writer,
                    duration=1,
                    feed="iex",
                )
            )
    finally:
        writer.close()

    assert shadow.records[-1].action == "failed"
    assert shadow.records[-1].disposition == reason


def test_local_relay_transport_preserves_direct_normalization_without_credentials(
    tmp_path: Path,
) -> None:
    payload = {
        "T": "b",
        "S": "AAPL",
        "o": 100.0,
        "h": 101.0,
        "l": 99.0,
        "c": 100.5,
        "v": 12.0,
        "t": at(0).isoformat().replace("+00:00", "Z"),
    }
    direct_socket = _FakeSocket(
        [
            line([{"T": "success", "msg": "connected"}]),
            line([{"T": "success", "msg": "authenticated"}]),
            line([{"T": "subscription", "bars": ["AAPL"], "quotes": ["AAPL"]}]),
            line([payload]),
        ]
    )
    local_socket = _FakeSocket(
        [
            line([{"T": "subscription", "bars": ["AAPL"], "quotes": ["AAPL"]}]),
            line([payload]),
        ]
    )
    direct_session = ShadowSession(config())
    local_session = ShadowSession(config())
    direct_writer = EvidenceWriter(tmp_path / "direct.jsonl", direct_session.config)
    local_writer = EvidenceWriter(tmp_path / "local.jsonl", local_session.config)
    try:
        with pytest.raises(OSError, match="test transport ended"):
            asyncio.run(
                consume(
                    direct_socket,
                    direct_session,
                    DataCredentials("key", "secret"),
                    direct_writer,
                    feed="iex",
                    clock=lambda: at(1),
                )
            )
        with pytest.raises(OSError, match="test transport ended"):
            asyncio.run(
                consume_local(
                    local_socket,
                    local_session,
                    local_writer,
                    feed="iex",
                    clock=lambda: at(1),
                )
            )
    finally:
        direct_writer.close()
        local_writer.close()

    assert direct_session.records[-1].observation == local_session.records[-1].observation
    assert [json.loads(message) for message in local_socket.sent] == [
        {"action": "subscribe", "bars": ["AAPL"], "quotes": ["AAPL"]}
    ]
    assert "secret" not in "".join(local_socket.sent)


@pytest.mark.parametrize(
    "url",
    [
        "wss://127.0.0.1:8765",
        "ws://example.invalid:8765",
        "ws://127.0.0.1:8765/health",
        "ws://127.0.0.1:8765/?secret=no",
    ],
)
def test_local_relay_url_must_be_credential_free_loopback_data_path(url: str) -> None:
    with pytest.raises(ValueError):
        _local_feed_url_checked(url)


def test_stream_uses_session_scope_for_auth_subscription_and_translation(tmp_path: Path) -> None:
    bar_payload = bar(0, "100")
    bar_payload.pop("received")
    for field in ("o", "h", "l", "c", "v"):
        value = bar_payload[field]
        assert isinstance(value, Decimal)
        bar_payload[field] = float(value)
    quote_payload = quote(0)
    quote_payload.pop("received")
    for field in ("bp", "ap", "bs", "as"):
        value = quote_payload[field]
        assert isinstance(value, Decimal)
        quote_payload[field] = float(value)
    socket = _FakeSocket(
        [
            line([{"T": "success", "msg": "connected"}]),
            line([{"T": "success", "msg": "authenticated"}]),
            line(
                [
                    {
                        "T": "subscription",
                        "bars": ["AAPL"],
                        "quotes": ["AAPL"],
                        "trades": [],
                        "updatedBars": [],
                        "dailyBars": [],
                        "statuses": [],
                        "lulds": [],
                        "corrections": [],
                        "cancelErrors": [],
                    }
                ]
            ),
            json.dumps([quote_payload, bar_payload]),
        ]
    )
    shadow = ShadowSession(config())
    path = tmp_path / "stream.jsonl"
    writer = EvidenceWriter(path, shadow.config)
    times = iter((at(0), at(2)))

    try:
        with pytest.raises(OSError, match="test transport ended"):
            asyncio.run(
                consume(
                    socket,
                    shadow,
                    DataCredentials("key", "secret"),
                    writer,
                    feed="iex",
                    clock=lambda: next(times),
                )
            )
    finally:
        writer.close()

    assert [json.loads(message)["action"] for message in socket.sent] == ["auth", "subscribe"]
    subscription = json.loads(socket.sent[1])
    assert subscription == {"action": "subscribe", "bars": ["AAPL"], "quotes": ["AAPL"]}
    assert [record.disposition for record in shadow.records] == [
        "connected",
        "accepted",
        "accepted",
    ]
    assert isinstance(shadow.records[1].observation, Quote)
    assert isinstance(shadow.records[2].observation, Bar)


def test_optional_pure_risk_evaluation_never_creates_admission_artifact() -> None:
    state = RiskState(
        "shadow-test-scope",
        "supplied-offline-state",
        1,
        True,
        (),
        (),
        at(3),
        at(3),
        OperatorControls(True, False, at(3), at(3)),
    )
    shadow = session(state=state)
    records = _two_bars_then_quote_then_final_bar(shadow)
    observability = records[-1].risk_observability
    assert observability is not None
    assert observability.status is RiskObservabilityStatus.EVALUATED
    assert observability.decision is not None
    assert all(record.__class__.__name__ == "ShadowRecord" for record in shadow.records)


def test_session_id_is_evidence_only_and_scope_is_causal_identity() -> None:
    base = replace(
        config(),
        session_id="capture-one",
        operational_scope="paper-primary",
        source_dataset_id="alpaca:iex:aapl-1m-v1",
    )

    def candidate(candidate_config: ShadowConfig) -> ShadowRecord:
        shadow = ShadowSession(candidate_config)
        shadow.control("connected", at(0))
        return _two_bars_then_quote_then_final_bar(shadow)[-1]

    first = candidate(base)
    restart = candidate(replace(base, session_id="capture-two"))
    other_scope = candidate(replace(base, operational_scope="paper-secondary"))
    assert first.intent is not None
    assert restart.intent is not None
    assert other_scope.intent is not None
    assert first.intent.intent_identity == restart.intent.intent_identity
    assert first.intent.intent_identity != other_scope.intent.intent_identity
    first_key = source_opportunity_key_for_intent(
        account_id="paper-account", operational_scope="paper-primary", intent=first.intent
    )
    restart_key = source_opportunity_key_for_intent(
        account_id="paper-account", operational_scope="paper-primary", intent=restart.intent
    )
    other_scope_key = source_opportunity_key_for_intent(
        account_id="paper-account", operational_scope="paper-secondary", intent=other_scope.intent
    )
    assert first_key.source_key == restart_key.source_key
    assert first_key.source_key != other_scope_key.source_key


def test_missing_credentials_and_no_gate_or_trading_imports() -> None:
    with pytest.raises(ValueError, match="ALPACA_DATA_KEY"):
        DataCredentials.from_environment({})
    p4a = Path("src/shadow/adapters/alpaca/stream.py").read_text(encoding="utf-8")
    composition = Path("src/shadow/application/shadow.py").read_text(encoding="utf-8")
    assert "RiskGate" not in composition and "claim_for_dispatch" not in composition
    assert "AuthorizedOrder" not in composition and "AuthorizedOrder" not in p4a
    assert "config.instrument" not in p4a
    assert all(
        forbidden not in p4a
        for forbidden in (
            "TradingClient",
            "submit_order",
            "cancel_order",
            "replace_order",
            "/account",
        )
    )
