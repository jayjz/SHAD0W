"""Counterexamples to pairing, omission, cost, numeric, and identity manipulation."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from itertools import permutations

import pytest

from shadow.data.fingerprint import quote_evidence_fingerprint
from shadow.domain import Instrument
from shadow.evaluation import (
    EVALUATION_MODEL_ID,
    EvaluationError,
    TradeEligibility,
    TradeRecord,
    build_manifest,
    evaluate_historical,
    reconstruct_trades,
)
from shadow.execution import ExecutionEconomicsConfig, ExecutionStatus, attach_execution_economics
from shadow.simulation import LifecycleState, SimulationInput, run_simulation
from tests.test_simulation_runner import (
    QQQ,
    SPY,
    START,
    _bar,
    _config,
    _input,
    _opportunity,
    _quote,
)

REVISION = "d4be72167c0554d8b2d8adc269a9aa2e0eacc0ca+p1a-test-build"


def _run_input(
    *,
    entry: str = "100",
    exit_price: str = "110",
    count: int = 3,
    instruments: tuple[Instrument, ...] = (SPY,),
) -> SimulationInput:
    bars = tuple(
        _bar(instrument, "10" if minute % 2 == 0 else "9", minute)
        for minute in range(count)
        for instrument in instruments
    )
    times = [START + timedelta(minutes=minute, seconds=1) for minute in range(1, count)]
    return replace(
        _input(
            bars,
            configs=tuple(_config(i) for i in instruments),
            quotes=tuple(
                _quote(
                    i,
                    bid=entry if n % 2 == 0 else exit_price,
                    ask=entry if n % 2 == 0 else exit_price,
                    observation_time=time,
                    availability_time=time,
                )
                for n, time in enumerate(times)
                for i in instruments
            ),
            opportunities=tuple(
                _opportunity(f"{i.identifier}-{n}", i, time)
                for n, time in enumerate(times)
                for i in instruments
            ),
        ),
        economics_configs=tuple(
            ExecutionEconomicsConfig(i, Decimal(2), "USD", Decimal(10)) for i in instruments
        ),
    )


@pytest.mark.parametrize(
    "exit_price,gross,fees,net,gross_return,net_return,wins,losses",
    [
        ("110", "20", ".42", "19.58", ".1", ".0979", 1, 0),
        ("90", "-20", ".38", "-20.38", "-.1", "-.1019", 0, 1),
        ("100", "0", ".4", "-.4", "0", "-.002", 0, 1),
    ],
)
def test_manual_trade_and_aggregate_arithmetic(
    exit_price: str,
    gross: str,
    fees: str,
    net: str,
    gross_return: str,
    net_return: str,
    wins: int,
    losses: int,
) -> None:
    source = run_simulation(_run_input(exit_price=exit_price))
    evaluation = evaluate_historical(source, code_revision=REVISION)
    (trade,) = evaluation.reconstruction.completed_trades
    assert trade.entry.execution_price == Decimal(100)
    assert trade.exit.execution_price == Decimal(exit_price)
    assert trade.quantity == Decimal(2)
    assert trade.entry.fee == Decimal(".2")
    assert trade.exit.fee == Decimal(exit_price) * Decimal(".002")
    assert trade.total_fees == Decimal(fees)
    assert trade.gross_result == Decimal(gross)
    assert trade.net_result == Decimal(net)
    assert trade.return_denominator == Decimal(200)
    assert trade.gross_return == Decimal(gross_return)
    assert trade.net_return == Decimal(net_return)
    assert trade.entry_time < trade.exit_time
    assert trade.closing_record.position is not None
    assert trade.closing_record.position.opening_action_id == trade.entry_action_reference
    assert trade.entry.attempt_reference != trade.exit.attempt_reference
    assert evaluation.completed_trade_count == evaluation.eligible_trade_count == 1
    assert evaluation.incomplete_trade_count == evaluation.stress_trade_count == 0
    (summary,) = evaluation.currency_summaries
    assert summary.gross_result_total == trade.gross_result
    assert summary.net_result_total == trade.net_result
    assert summary.total_fees == trade.total_fees
    assert (summary.winning_trade_count, summary.losing_trade_count, summary.flat_trade_count) == (
        wins,
        losses,
        0,
    )


def test_zero_fees_flat_net_is_explicit() -> None:
    source = _run_input(exit_price="100")
    assert source.economics_configs is not None
    source = replace(
        source, economics_configs=(replace(source.economics_configs[0], fee_bps=Decimal(0)),)
    )
    (summary,) = evaluate_historical(
        run_simulation(source), code_revision=REVISION
    ).currency_summaries
    assert summary.flat_trade_count == 1
    assert summary.net_result_total == 0


def test_spread_and_slippage_are_embedded_exactly_once() -> None:
    source = _run_input()
    source = replace(
        source,
        quotes=(
            replace(source.quotes[0], bid_price=Decimal(98)),
            replace(source.quotes[1], ask_price=Decimal(112)),
        ),
        execution_config=replace(source.execution_config, slippage_bps=Decimal(10)),
    )
    (trade,) = reconstruct_trades(run_simulation(source)).completed_trades
    assert trade.entry.execution_price == Decimal("100.1")
    assert trade.exit.execution_price == Decimal("109.89")
    assert trade.gross_result == Decimal("19.58")
    assert trade.entry.fee == Decimal(".2002")
    assert trade.exit.fee == Decimal(".21978")
    assert trade.net_result == Decimal("19.16002")


def test_entry_and_exit_retries_preserve_failed_evidence_without_trade_inflation() -> None:
    source = _run_input()
    entry_time, exit_time = (q.availability_time for q in source.quotes)
    first = entry_time - timedelta(microseconds=1)
    crossed_entry = replace(
        source.quotes[0], bid_price=Decimal(101), observation_time=first, availability_time=first
    )
    crossed_exit = replace(
        source.quotes[1],
        bid_price=Decimal(111),
        observation_time=exit_time,
        availability_time=exit_time,
    )
    retry_time = exit_time + timedelta(seconds=1)
    source = replace(
        source,
        execution_config=replace(source.execution_config, maximum_quote_age=timedelta(seconds=1)),
        quotes=(
            crossed_entry,
            source.quotes[0],
            crossed_exit,
            replace(source.quotes[1], observation_time=retry_time, availability_time=retry_time),
        ),
        execution_opportunities=(
            _opportunity("missing", SPY, first - timedelta(microseconds=1)),
            _opportunity("crossed-entry", SPY, first),
            source.execution_opportunities[0],
            _opportunity("stale-exit", SPY, exit_time - timedelta(microseconds=1)),
            source.execution_opportunities[1],
            _opportunity("exit-retry", SPY, retry_time),
        ),
    )
    result = run_simulation(source)
    reconstruction = reconstruct_trades(result)
    assert [o.status for o in result.execution_outcomes] == [
        ExecutionStatus.UNFILLED,
        ExecutionStatus.REJECTED,
        ExecutionStatus.FILLED,
        ExecutionStatus.UNFILLED,
        ExecutionStatus.REJECTED,
        ExecutionStatus.FILLED,
    ]
    assert len(reconstruction.completed_trades) == 1
    assert len(reconstruction.unsuccessful_outcomes) == 4
    trade = reconstruction.completed_trades[0]
    assert trade.entry.outcome == result.execution_outcomes[2]
    assert trade.exit.outcome == result.execution_outcomes[5]


@pytest.mark.parametrize(
    "state", [LifecycleState.PENDING_ENTRY, LifecycleState.HOLDING, LifecycleState.PENDING_EXIT]
)
def test_end_of_stream_does_not_manufacture_closure(state: LifecycleState) -> None:
    source = _run_input(count=3 if state is LifecycleState.PENDING_EXIT else 2)
    source = replace(
        source,
        execution_opportunities=(
            () if state is LifecycleState.PENDING_ENTRY else source.execution_opportunities[:1]
        ),
    )
    result = run_simulation(source)
    evaluation = evaluate_historical(result, code_revision=REVISION)
    assert evaluation.completed_trade_count == 0
    assert evaluation.incomplete_trade_count == 1
    assert evaluation.reconstruction.incomplete_states == result.lifecycle.final_states
    assert evaluation.reconstruction.incomplete_states[0].state is state
    assert len(evaluation.reconstruction.open_entries) == (
        state is not LifecycleState.PENDING_ENTRY
    )
    assert evaluation.currency_summaries == ()


@pytest.mark.parametrize("field,value", [("quantity", Decimal(3)), ("quote_currency", "EUR")])
def test_mismatched_quantity_currency_fail_in_reconstruction_and_trade_contract(
    field: str, value: object
) -> None:
    source = run_simulation(_run_input())
    (trade,) = reconstruct_trades(source).completed_trades
    if field == "quantity":
        assert isinstance(value, Decimal)
        config = replace(trade.exit.config, quantity=value)
    else:
        assert isinstance(value, str)
        config = replace(trade.exit.config, quote_currency=value)
    changed = attach_execution_economics(trade.exit.outcome, config)
    assert changed is not None
    with pytest.raises(EvaluationError, match="quantity|currency"):
        replace(trade, exit=changed)
    with pytest.raises(EvaluationError, match="quantity/currency/fee"):
        reconstruct_trades(replace(source, economic_executions=(trade.entry, changed)))


def test_instrument_isolation_and_currency_totals_never_mix_units() -> None:
    source = _run_input(instruments=(SPY, QQQ))
    assert source.economics_configs is not None
    source = replace(
        source,
        economics_configs=tuple(
            replace(config, quote_currency="EUR" if config.instrument == QQQ else "USD")
            for config in source.economics_configs
        ),
    )
    result = run_simulation(source)
    evaluation = evaluate_historical(result, code_revision=REVISION)
    assert evaluation.completed_trade_count == 2
    assert [s.quote_currency for s in evaluation.currency_summaries] == ["EUR", "USD"]
    assert all(s.net_result_total == Decimal("19.58") for s in evaluation.currency_summaries)
    first, second = evaluation.reconstruction.completed_trades
    with pytest.raises(EvaluationError, match="opening position"):
        TradeRecord(first.entry, second.exit, first.opening_record, second.closing_record)


def test_all_incidental_evidence_orderings_preserve_pairing_and_manifest() -> None:
    result = run_simulation(_run_input(count=5, instruments=(SPY, QQQ)))
    expected = evaluate_historical(result, code_revision=REVISION)
    lifecycle = result.lifecycle
    reordered = replace(
        result,
        quotes=tuple(reversed(result.quotes)),
        strategy_configs=tuple(reversed(result.strategy_configs)),
        execution_attempts=tuple(reversed(result.execution_attempts)),
        execution_outcomes=tuple(reversed(result.execution_outcomes)),
        economic_executions=tuple(reversed(result.economic_executions)),
        timeline=replace(
            result.timeline, ordered_events=tuple(reversed(result.timeline.ordered_events))
        ),
        lifecycle=replace(
            lifecycle,
            ordered_events=tuple(reversed(lifecycle.ordered_events)),
            records=tuple(reversed(lifecycle.records)),
            final_states=tuple(reversed(lifecycle.final_states)),
            execution_attempts=tuple(reversed(lifecycle.execution_attempts)),
            execution_outcomes=tuple(reversed(lifecycle.execution_outcomes)),
        ),
    )
    assert evaluate_historical(reordered, code_revision=REVISION) == expected
    for economic_order in permutations(result.economic_executions[:4]):
        changed = replace(
            result, economic_executions=(*economic_order, *result.economic_executions[4:])
        )
        assert reconstruct_trades(changed) == expected.reconstruction


@pytest.mark.parametrize(
    "field", ["economic_executions", "execution_outcomes", "execution_attempts"]
)
def test_duplicate_or_missing_execution_evidence_is_not_silently_counted(field: str) -> None:
    result = run_simulation(_run_input())
    values = getattr(result, field)
    for changed in ((*values, values[0]), values[1:]):
        with pytest.raises(EvaluationError):
            reconstruct_trades(replace(result, **{field: changed}))  # type: ignore[arg-type]


def test_duplicate_closure_and_lifecycle_omission_are_contract_failures() -> None:
    result = run_simulation(_run_input())
    records = result.lifecycle.records
    for changed in ((*records, records[-1]), records[:-1]):
        with pytest.raises(EvaluationError):
            reconstruct_trades(
                replace(result, lifecycle=replace(result.lifecycle, records=changed))
            )


def test_open_positions_cannot_disappear_from_summaries() -> None:
    result = run_simulation(_run_input(count=2))
    with pytest.raises(EvaluationError, match="open positions"):
        reconstruct_trades(replace(result, lifecycle=replace(result.lifecycle, open_positions=())))
    with pytest.raises(EvaluationError, match="final states"):
        reconstruct_trades(replace(result, lifecycle=replace(result.lifecycle, final_states=())))


def test_future_extension_preserves_completed_trade_records() -> None:
    prefix = evaluate_historical(run_simulation(_run_input()), code_revision=REVISION)
    extended = evaluate_historical(run_simulation(_run_input(count=5)), code_revision=REVISION)
    assert len(extended.reconstruction.completed_trades) == 2
    assert extended.reconstruction.completed_trades[:1] == prefix.reconstruction.completed_trades
    assert extended.manifest.identity != prefix.manifest.identity


@pytest.mark.parametrize(
    "entry,exit_price", [("0", "10"), ("-10", "-5"), ("100", "0"), ("100", "-5")]
)
def test_stress_evidence_has_economics_but_no_ordinary_returns_or_metrics(
    entry: str, exit_price: str
) -> None:
    evaluation = evaluate_historical(
        run_simulation(_run_input(entry=entry, exit_price=exit_price)), code_revision=REVISION
    )
    (trade,) = evaluation.reconstruction.completed_trades
    assert trade.eligibility is TradeEligibility.STRESS_PRICE
    assert trade.net_return is trade.gross_return is trade.return_denominator is None
    assert trade.gross_result == (Decimal(exit_price) - Decimal(entry)) * 2
    assert trade.total_fees >= 0
    assert evaluation.completed_trade_count == evaluation.stress_trade_count == 1
    assert evaluation.eligible_trade_count == 0
    (summary,) = evaluation.currency_summaries
    assert summary.eligible_trade_count == 0
    assert summary.net_result_total == summary.total_fees == summary.gross_result_total == 0
    assert (
        summary.winning_trade_count == summary.losing_trade_count == summary.flat_trade_count == 0
    )


def test_hostile_decimal_context_and_upstream_non_authority() -> None:
    result = run_simulation(_run_input(entry="100.1234567890123456789012345678901234"))
    before = repr(result)
    expected = evaluate_historical(result, code_revision=REVISION)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emin = -2
        context.Emax = 2
        context.capitals = 0
        context.traps[Inexact] = True
        actual = evaluate_historical(result, code_revision=REVISION)
        assert actual.manifest.identity == expected.manifest.identity
        assert (
            actual.currency_summaries[0].net_result_total
            == expected.currency_summaries[0].net_result_total
        )
    assert actual == expected
    assert repr(result) == before
    (trade,) = actual.reconstruction.completed_trades
    assert trade.entry is result.economic_executions[0]
    with pytest.raises(FrozenInstanceError):
        trade.entry = trade.exit  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        actual.manifest.code_revision = "other"  # type: ignore[misc]


def test_experiment_identity_covers_material_assumptions_and_unused_evidence() -> None:
    source = _run_input()
    result = run_simulation(source)
    baseline = build_manifest(result, code_revision=REVISION)
    assert source.economics_configs is not None
    variants = [
        replace(
            source,
            strategy_configs=(
                replace(source.strategy_configs[0], entry_threshold=Decimal("-1.1")),
            ),
        ),
        replace(source, strategy_configs=(replace(source.strategy_configs[0], rolling_window=3),)),
        replace(source, execution_config=replace(source.execution_config, slippage_bps=Decimal(1))),
        replace(
            source,
            execution_config=replace(
                source.execution_config, maximum_quote_age=timedelta(seconds=1)
            ),
        ),
        replace(
            source, economics_configs=(replace(source.economics_configs[0], quantity=Decimal(3)),)
        ),
        replace(
            source, economics_configs=(replace(source.economics_configs[0], fee_bps=Decimal(0)),)
        ),
        replace(
            source, economics_configs=(replace(source.economics_configs[0], quote_currency="EUR"),)
        ),
        replace(source, bars=(_bar(SPY, "11", 0), *source.bars[1:])),
        replace(
            source, dataset_metadata=replace(source.dataset_metadata, dataset_id="another-dataset")
        ),
        replace(source, quotes=(*source.quotes, _quote(QQQ))),
        replace(
            source,
            execution_opportunities=(
                *source.execution_opportunities,
                _opportunity("unused", QQQ, START),
            ),
        ),
    ]
    manifests = [
        build_manifest(run_simulation(variant), code_revision=REVISION) for variant in variants
    ]
    assert all(manifest.identity != baseline.identity for manifest in manifests)
    assert manifests[-2].quote_evidence_fingerprint != baseline.quote_evidence_fingerprint
    assert (
        manifests[-1].opportunity_evidence_fingerprint != baseline.opportunity_evidence_fingerprint
    )
    for variant in (
        replace(result, feature_implementation_version="different-feature"),
        replace(result, simulation_implementation_version="different-simulator"),
    ):
        assert build_manifest(variant, code_revision=REVISION).identity != baseline.identity
    assert build_manifest(result, code_revision="other-code").identity != baseline.identity
    assert (
        build_manifest(result, code_revision=REVISION, limitations=("synthetic fixture",)).identity
        != baseline.identity
    )
    assert baseline.evaluation_model_id == EVALUATION_MODEL_ID
    with pytest.raises(EvaluationError):
        replace(baseline, evaluation_model_id="unimplemented-version")
    with pytest.raises(EvaluationError):
        build_manifest(result, code_revision="")


def test_quote_evidence_identity_is_complete_and_context_independent() -> None:
    quote = _quote(bid="100.00000000000000000000000000000001", ask="101")
    baseline = quote_evidence_fingerprint((quote,))
    for changed in (
        replace(quote, bid_price=Decimal("100.00000000000000000000000000000002")),
        replace(quote, bid_size=Decimal(1)),
        replace(quote, availability_time=quote.availability_time + timedelta(seconds=1)),
        replace(quote, provenance=replace(quote.provenance, session="different")),
        replace(quote, ask_price=Decimal("101.00")),
    ):
        assert quote_evidence_fingerprint((changed,)) != baseline
        assert quote_evidence_fingerprint((quote, changed)) == quote_evidence_fingerprint(
            (changed, quote)
        )
    assert quote_evidence_fingerprint(()) != baseline
    assert quote_evidence_fingerprint((quote, quote)) != baseline
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert quote_evidence_fingerprint((quote,)) == baseline


def test_price_only_run_requires_explicit_economics() -> None:
    result = run_simulation(replace(_run_input(), economics_configs=None))
    with pytest.raises(EvaluationError, match="enabled economics"):
        evaluate_historical(result, code_revision=REVISION)


def test_small_price_fee_loops_obey_trade_invariants() -> None:
    for exit_price in ("90", "100", "110"):
        for fee in ("0", "10", "10000"):
            source = _run_input(exit_price=exit_price)
            assert source.economics_configs is not None
            source = replace(
                source,
                economics_configs=(replace(source.economics_configs[0], fee_bps=Decimal(fee)),),
            )
            result = run_simulation(source)
            (trade,) = reconstruct_trades(result).completed_trades
            assert trade.net_result == trade.gross_result - trade.total_fees
            assert trade.total_fees >= 0
            assert trade.exit_time >= trade.entry_time
            assert (
                len(reconstruct_trades(result).completed_trades)
                <= len(result.economic_executions) // 2
            )


def test_stale_fill_cannot_be_presented_as_an_ordinary_trade() -> None:
    """ExecutionOutcome validates price, but not the quote's causal freshness."""
    result = run_simulation(_run_input())
    (trade,) = reconstruct_trades(result).completed_trades
    original_quote = trade.entry.outcome.market_evidence
    assert original_quote is not None
    stale = replace(original_quote, observation_time=START - timedelta(days=2))
    outcome = replace(trade.entry.outcome, market_evidence=stale)
    economic = replace(trade.entry, outcome=outcome)
    with pytest.raises(EvaluationError, match="stale"):
        replace(
            trade,
            entry=economic,
            opening_record=replace(trade.opening_record, execution_outcome=outcome),
        )


def test_fill_execution_model_must_match_its_declared_configuration() -> None:
    (trade,) = reconstruct_trades(run_simulation(_run_input())).completed_trades
    attempt = replace(trade.entry.outcome.attempt, execution_model_id="different-model")
    outcome = replace(trade.entry.outcome, attempt=attempt)
    economic = replace(trade.entry, outcome=outcome)
    with pytest.raises(EvaluationError, match="execution model"):
        replace(
            trade,
            entry=economic,
            opening_record=replace(
                trade.opening_record, execution_attempt=attempt, execution_outcome=outcome
            ),
        )


@pytest.mark.parametrize(
    "entry,exit_price,quantity",
    [
        ("-9e999999", "9e999999", "1"),
        ("1e-999999", "1e999999", "1"),
        ("1e-999999", "1.00000000000000000000000000000000001e-999999", "1e999999"),
    ],
)
def test_evaluation_numeric_domain_failures_are_explicit(
    entry: str,
    exit_price: str,
    quantity: str,
) -> None:
    source = _run_input(entry=entry, exit_price=exit_price)
    source = replace(
        source,
        economics_configs=(ExecutionEconomicsConfig(SPY, Decimal(quantity), "USD", Decimal(0)),),
    )
    result = run_simulation(source)
    assert len(result.economic_executions) == 2
    with pytest.raises(EvaluationError, match="numeric domain"):
        reconstruct_trades(result)


def test_rebinding_second_exit_to_first_entry_fails_even_with_valid_individual_fills() -> None:
    result = run_simulation(_run_input(count=5))
    first, second = reconstruct_trades(result).completed_trades
    false_close = replace(second.closing_record, position=first.closing_record.position)
    records = tuple(
        false_close if record == second.closing_record else record
        for record in result.lifecycle.records
    )
    with pytest.raises(EvaluationError, match="authoritative open position"):
        reconstruct_trades(replace(result, lifecycle=replace(result.lifecycle, records=records)))


def test_future_quote_cannot_be_hidden_inside_a_completed_trade() -> None:
    (trade,) = reconstruct_trades(run_simulation(_run_input())).completed_trades
    quote = trade.entry.outcome.market_evidence
    assert quote is not None
    quote = replace(quote, availability_time=trade.exit_time)
    outcome = replace(trade.entry.outcome, market_evidence=quote)
    economic = replace(trade.entry, outcome=outcome)
    with pytest.raises(EvaluationError, match="future"):
        replace(
            trade,
            entry=economic,
            opening_record=replace(trade.opening_record, execution_outcome=outcome),
        )


def test_trade_cannot_close_before_its_entry() -> None:
    (trade,) = reconstruct_trades(run_simulation(_run_input())).completed_trades
    action = trade.closing_record.action
    assert action is not None
    early = START + timedelta(seconds=1)
    action = replace(action, created_time=START, eligibility_after_time=START)
    attempt = replace(
        trade.exit.outcome.attempt,
        attempt_time=early,
        opportunity_time=early,
        eligibility_after_time=START,
    )
    quote = trade.exit.outcome.market_evidence
    assert quote is not None
    quote = replace(quote, observation_time=START, availability_time=START)
    outcome = replace(trade.exit.outcome, attempt=attempt, market_evidence=quote)
    economic = replace(trade.exit, outcome=outcome)
    record = replace(
        trade.closing_record,
        action=action,
        execution_attempt=attempt,
        execution_outcome=outcome,
        event_time=early,
    )
    with pytest.raises(EvaluationError, match="causally"):
        replace(trade, exit=economic, closing_record=record)
