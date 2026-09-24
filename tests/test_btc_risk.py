"""Pure BTC risk authority with explicit evidence; never external transport."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from shadow.domain.crypto_market import CryptoQuote, UtcNanoseconds
from shadow.domain.market import AvailabilitySemantics, Provenance
from shadow.execution.broker import (
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Eligibility,
    Evidence,
    OrderStatus,
    SubmissionResult,
    SubmissionStatus,
)
from shadow.execution.crypto import BtcCashAccount
from shadow.features.btc_trend import BTC, HOUR_NS
from shadow.risk.btc import evaluate_btc_risk, linked_entry_ns, utc_ns
from shadow.risk.btc_models import BtcRiskPolicy
from shadow.risk.models import OperatorControls, OrderSide, OrderTarget, OrderType, TimeInForce
from shadow.strategies.btc_trend import engineering_canary, propose
from tests.test_btc_execution import asset, btc_request
from tests.test_btc_trend import rising
from tests.test_paper_dispatch import _dispatch_with_historical_status
from tests.test_paper_reconciliation import NOW


def inputs() -> dict[str, Any]:
    base = utc_ns(NOW) - 73 * HOUR_NS
    intervals = tuple(
        replace(
            row,
            start_ns=row.start_ns + base,
            end_ns=row.end_ns + base,
            available_ns=row.available_ns + base,
        )
        for row in rising()
    )
    now_ns = utc_ns(NOW) + 1
    config = engineering_canary()
    feature = config.features(intervals, now_ns)
    signal = propose(config, feature, now_ns=now_ns, holding=False)
    assert signal is not None
    ev = Evidence("fresh", "paper-account", "scope", NOW, NOW)
    return dict(
        policy=BtcRiskPolicy(
            "paper-account",
            "scope",
            Decimal("0.01"),
            Decimal(100),
            Decimal(1),
            30_000_000_000,
            30_000_000_000,
            30_000_000_000,
        ),
        config=config,
        proposal=signal,
        request=btc_request(),
        intervals=intervals,
        quote=CryptoQuote(
            BTC,
            Decimal(172),
            Decimal(173),
            Decimal(1),
            Decimal(1),
            UtcNanoseconds(now_ns),
            UtcNanoseconds(now_ns),
            AvailabilitySemantics.SYSTEM_RECEIVED,
            Provenance("synthetic"),
        ),
        account=BtcCashAccount(
            ev,
            OrderTarget.PAPER,
            Eligibility.ELIGIBLE,
            Decimal(1000),
            "USD",
            available_cash=Decimal(100),
            crypto_trading=Eligibility.ELIGIBLE,
        ),
        asset=asset(),
        snapshot=BrokerSnapshot(ev, (), (), True, True, NOW - timedelta(hours=73), NOW),
        attempts=(),
        controls=OperatorControls(True, False, NOW, NOW),
        now_ns=now_ns,
    )


def test_entry_authorized_only_from_recomputed_flat_evidence() -> None:
    values = inputs()
    assert evaluate_btc_risk(**values).authorized
    assert evaluate_btc_risk(**values) == evaluate_btc_risk(**inputs())
    values["proposal"] = replace(
        values["proposal"],
        features=replace(values["proposal"].features, trend_distance=Decimal(100)),
    )
    assert "invalid_strategy_evidence" in evaluate_btc_risk(**values).reasons


@pytest.mark.parametrize(
    "failure",
    [
        "disabled",
        "kill",
        "cash",
        "asset",
        "stale_quote",
        "future_quote",
        "stale_asset",
        "scope",
        "quantity",
        "cost",
        "gap",
        "unresolved",
        "halted",
    ],
)
def test_entry_rejections(failure: str) -> None:
    values = inputs()
    if failure in ("disabled", "kill"):
        values["controls"] = OperatorControls(failure != "disabled", failure == "kill", NOW, NOW)
    elif failure == "cash":
        values["account"] = replace(values["account"], available_cash=Decimal(0))
    elif failure == "asset":
        values["asset"] = replace(values["asset"], tradable=Eligibility.INELIGIBLE)
    elif failure in ("stale_quote", "future_quote"):
        timestamp = values["now_ns"] + (1 if failure == "future_quote" else -31_000_000_000)
        values["quote"] = replace(
            values["quote"],
            observation_time=UtcNanoseconds(timestamp),
            availability_time=UtcNanoseconds(timestamp),
        )
    elif failure == "stale_asset":
        ev = replace(values["asset"].evidence, observation_time=NOW - timedelta(minutes=1))
        values["asset"] = replace(values["asset"], evidence=ev)
    elif failure == "scope":
        values["policy"] = replace(values["policy"], operational_scope="wrong")
    elif failure == "quantity":
        values["request"] = replace(values["request"], quantity=Decimal("0.00121"))
    elif failure == "cost":
        values["proposal"] = replace(values["proposal"], round_trip_cost=Decimal(0))
    elif failure == "gap":
        values["intervals"] = values["intervals"][:30] + values["intervals"][31:]
    elif failure == "unresolved":
        values["snapshot"] = replace(values["snapshot"], orders_complete=False)
    else:
        values["snapshot"] = replace(
            values["snapshot"],
            positions=(BrokerPosition(values["snapshot"].evidence, BTC, Decimal("0.1")),),
        )
    assert not evaluate_btc_risk(**values).authorized


def holding_inputs(tmp_path: Path) -> dict[str, Any]:
    values = inputs()
    outcome, _ = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    assert outcome.attempt is not None
    request = replace(btc_request(), client_id=outcome.attempt.client_order_id)
    ev = values["snapshot"].evidence
    filled = BrokerOrder(
        ev,
        "buy",
        request.client_id,
        BTC,
        OrderSide.BUY,
        request.quantity,
        request.quantity,
        OrderStatus.FILLED,
        OrderType.MARKET,
        TimeInForce.GTC,
        False,
        None,
        None,
    )
    attempt = replace(
        outcome.attempt,
        request=request,
        committed_at=NOW - timedelta(hours=25),
        submission=SubmissionResult(ev, request, SubmissionStatus.ACCEPTED, filled, None),
    )
    values["attempts"] = (attempt,)
    values["snapshot"] = replace(
        values["snapshot"], orders=(filled,), positions=(BrokerPosition(ev, BTC, request.quantity),)
    )
    fill_ev = replace(
        ev, observation_time=NOW - timedelta(hours=24), availability_time=NOW - timedelta(hours=24)
    )
    values["fills"] = (
        BrokerFill(fill_ev, "execution", "buy", BTC, OrderSide.BUY, request.quantity, Decimal(148)),
    )
    values["proposal"] = propose(
        values["config"],
        values["proposal"].features,
        now_ns=values["now_ns"],
        holding=True,
        high_water_mark=Decimal(172),
        independent_risk_halt=True,
    )
    values["request"] = replace(request, side=OrderSide.SELL, client_id="exit")
    values["independent_risk_halt"] = True
    return values


def test_holding_exit_requires_complete_fills_and_never_exceeds_exposure(tmp_path: Path) -> None:
    values = holding_inputs(tmp_path)
    assert evaluate_btc_risk(**values).authorized
    # Same persisted evidence reconstructs the same entry/high-water after restart.
    from shadow.execution.reconciliation import reconcile

    state = reconcile(attempts=values["attempts"], snapshot=values["snapshot"])
    assert linked_entry_ns(
        fills=values["fills"], snapshot=values["snapshot"], reconciliation=state
    ) == utc_ns(NOW - timedelta(hours=24))
    from shadow.execution.journal_codec import canonical_bytes, decode_canonical

    # Durable canonical evidence can reconstruct the exit calculation. This is
    # file round-trip coverage, not SQLite/application restart acceptance.
    evidence_path = tmp_path / "btc-exit-evidence.bin"
    evidence_path.write_bytes(canonical_bytes((values["intervals"], values["fills"])))
    recovered = decode_canonical(evidence_path.read_bytes())
    assert isinstance(recovered, tuple)
    assert evaluate_btc_risk(**dict(values, intervals=recovered[0], fills=recovered[1])).authorized
    evaluation = evaluate_btc_risk(**values)
    assert decode_canonical(canonical_bytes(evaluation)) == evaluation
    assert not evaluate_btc_risk(**dict(values, fills=())).authorized
    assert not evaluate_btc_risk(**dict(values, fills=values["fills"] * 2)).authorized
    assert not evaluate_btc_risk(
        **dict(values, request=replace(values["request"], quantity=Decimal("0.0013")))
    ).authorized
    assert not evaluate_btc_risk(**dict(values, independent_risk_halt=False)).authorized


@pytest.mark.parametrize("state", ["entry_pending", "exit_pending", "unresolved", "halted"])
def test_lifecycle_states_cannot_authorize(tmp_path: Path, state: str) -> None:
    values = holding_inputs(tmp_path)
    snapshot = values["snapshot"]
    buy = snapshot.orders[0]
    if state == "entry_pending":
        values["snapshot"] = replace(
            snapshot,
            orders=(
                replace(
                    buy, status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal("0.0006")
                ),
            ),
            positions=(replace(snapshot.positions[0], quantity=Decimal("0.0006")),),
        )
    elif state == "exit_pending":
        attempt = replace(values["attempts"][0], request=values["request"], client_order_id="exit")
        exit_order = replace(
            buy,
            order_id="exit",
            client_id="exit",
            side=OrderSide.SELL,
            status=OrderStatus.NEW,
            filled_quantity=Decimal(0),
        )
        values["attempts"] += (attempt,)
        values["snapshot"] = replace(snapshot, orders=(buy, exit_order))
    elif state == "unresolved":
        values["snapshot"] = replace(snapshot, orders_complete=False)
    else:
        values["snapshot"] = replace(snapshot, positions=())
    result = evaluate_btc_risk(**values)
    assert result.reconciliation_state == state
    assert not result.authorized


def test_uncertain_attempt_freezes_even_with_later_linked_order(tmp_path: Path) -> None:
    values = holding_inputs(tmp_path)
    values["attempts"] = (replace(values["attempts"][0], submission=None),)
    result = evaluate_btc_risk(**values)
    assert result.reconciliation_state == "holding"
    assert "uncertain_submission" in result.reasons
