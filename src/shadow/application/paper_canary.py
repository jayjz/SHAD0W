"""Explicit one-shot Alpaca PAPER canary command; it is never a trading daemon."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from shadow.adapters.alpaca.paper_broker import AlpacaPaperBroker, PaperCredentials
from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.domain import Instrument
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    SubmitRequest,
)
from shadow.execution.dispatch import CanaryDispatcher
from shadow.execution.journal import ExecutionJournal
from shadow.execution.journal_codec import decode_canonical
from shadow.execution.ownership import AccountOwner
from shadow.risk.models import OrderSide, OrderTarget, OrderType, RiskDecision, TimeInForce

_ARMING_PHRASE = "I-UNDERSTAND-THIS-SUBMITS-ONE-ALPACA-PAPER-ORDER"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="one-shot SHAD0W Alpaca PAPER canary")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--operational-scope", required=True)
    parser.add_argument("--ownership-directory", type=Path, required=True)
    parser.add_argument("--journal-path", type=Path, required=True)
    parser.add_argument("--evidence-path", type=Path, required=True)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--arm-paper-order", action="store_true")
    parser.add_argument("--paper-acknowledgement")
    parser.add_argument("--risk-decision-path", type=Path)
    parser.add_argument("--source-opportunity-id")
    parser.add_argument("--daily-submission-limit", type=int)
    return parser


def _evidence(value: object) -> dict[str, object]:
    evidence = value.evidence  # type: ignore[attr-defined]
    return {
        "reference": evidence.reference,
        "account_id": evidence.account_id,
        "operational_scope": evidence.operational_scope,
        "observation_time": evidence.observation_time.isoformat(),
        "availability_time": evidence.availability_time.isoformat(),
    }


def _value(value: object, *, request: str) -> dict[str, object]:
    if isinstance(value, BrokerError):
        return {
            "request": request,
            "status": "error",
            "error_category": value.category.value,
            "reason": value.reason,
            "evidence": _evidence(value),
        }
    if isinstance(value, BrokerAccount):
        return {
            "request": request,
            "status": "ok",
            "target": value.target.value,
            "eligibility": value.eligibility.value,
            "buying_power": None if value.buying_power is None else str(value.buying_power),
            "currency": value.currency,
            "provider_account_id": value.provider_account_id,
            "evidence": _evidence(value),
        }
    if isinstance(value, BrokerClock):
        return {
            "request": request,
            "status": "ok",
            "state": value.state.value,
            "trading_date": value.trading_date.isoformat(),
            "session_open": None if value.session_open is None else value.session_open.isoformat(),
            "session_close": None
            if value.session_close is None
            else value.session_close.isoformat(),
            "evidence": _evidence(value),
        }
    if isinstance(value, BrokerAsset):
        return {
            "request": request,
            "status": "ok",
            "instrument": value.instrument.identifier,
            "us_equity": value.us_equity.value,
            "tradable": value.tradable.value,
            "evidence": _evidence(value),
        }
    if isinstance(value, BrokerPosition):
        return {
            "instrument": value.instrument.identifier,
            "quantity": str(value.quantity),
            "evidence": _evidence(value),
        }
    if isinstance(value, BrokerOrder):
        return {
            "order_id": value.order_id,
            "client_order_id": value.client_id,
            "instrument": value.instrument.identifier,
            "side": value.side.value,
            "quantity": str(value.quantity),
            "filled_quantity": str(value.filled_quantity),
            "status": value.status.value,
            "order_type": None if value.order_type is None else value.order_type.value,
            "time_in_force": None if value.time_in_force is None else value.time_in_force.value,
            "extended_hours": value.extended_hours,
            "replaces": value.replaces,
            "replaced_by": value.replaced_by,
            "evidence": _evidence(value),
        }
    if isinstance(value, BrokerSnapshot):
        return {
            "request": request,
            "status": "ok",
            "positions_complete": value.positions_complete,
            "orders_complete": value.orders_complete,
            "complete": value.complete,
            "history_start": value.history_start.isoformat(),
            "history_end": value.history_end.isoformat(),
            "positions": [_value(row, request="/v2/positions") for row in value.positions],
            "orders": [_value(row, request="/v2/orders") for row in value.orders],
            "evidence": _evidence(value),
        }
    raise TypeError(f"unsupported read-only evidence type: {type(value).__name__}")


def _read_only(broker: AlpacaPaperBroker, instrument: Instrument) -> dict[str, object]:
    account = broker.read_account()
    clock = broker.read_clock()
    asset = broker.read_asset(instrument)
    snapshot = broker.read_snapshot()
    return {
        "mode": "read_only",
        "target": "paper",
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": instrument.identifier,
        "account": _value(account, request="/v2/account"),
        "clock": _value(clock, request="/v2/clock"),
        "asset": _value(asset, request=f"/v2/assets/{instrument.identifier}"),
        "snapshot": _value(snapshot, request="/v2/positions + /v2/orders"),
    }


def _decision(path: Path) -> RiskDecision:
    try:
        decoded = decode_canonical(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise SystemExit("risk decision evidence is unreadable or noncanonical") from exc
    if not isinstance(decoded, RiskDecision):
        raise SystemExit("risk decision evidence must contain a SHAD0W RiskDecision")
    return decoded


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.read_only == arguments.arm_paper_order:
        raise SystemExit("choose exactly one of --read-only or --arm-paper-order")
    credentials = PaperCredentials.from_environment()
    instrument = Instrument(arguments.symbol)
    broker = AlpacaPaperBroker(
        credentials=credentials,
        account_id=arguments.account_id,
        operational_scope=arguments.operational_scope,
    )
    if arguments.read_only:
        _write(arguments.evidence_path, _read_only(broker, instrument))
        return 0
    if arguments.paper_acknowledgement != _ARMING_PHRASE:
        raise SystemExit("explicit PAPER acknowledgement is required")
    if arguments.risk_decision_path is None or arguments.source_opportunity_id is None:
        raise SystemExit(
            "armed canary requires durable risk decision and source opportunity evidence"
        )
    if arguments.daily_submission_limit is None:
        raise SystemExit("armed canary requires an explicit daily submission ceiling")
    decision = _decision(arguments.risk_decision_path)
    if decision.intent.side is not OrderSide.BUY or decision.intent.instrument != instrument:
        raise SystemExit("first canary accepts only its matching BUY risk decision")
    identity = derive_paper_client_order_identity(
        stable_account_binding=arguments.account_id,
        operational_scope=arguments.operational_scope,
        intent_identity=decision.intent.intent_identity,
    )
    request = SubmitRequest(
        arguments.account_id,
        arguments.operational_scope,
        identity.client_order_id,
        instrument,
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    arguments.ownership_directory.mkdir(parents=True, exist_ok=True)
    with AccountOwner.acquire(
        ownership_directory=arguments.ownership_directory, account_id=arguments.account_id
    ) as owner:
        if arguments.journal_path.exists():
            journal = ExecutionJournal.reopen(
                path=arguments.journal_path,
                owner=owner,
                account_id=arguments.account_id,
                operational_scope=arguments.operational_scope,
            )
        else:
            journal = ExecutionJournal.create(
                path=arguments.journal_path,
                owner=owner,
                account_id=arguments.account_id,
                operational_scope=arguments.operational_scope,
                created_at=datetime.now(UTC),
            )
        with journal:
            outcome = CanaryDispatcher(
                broker=broker,
                journal=journal,
                now=lambda: datetime.now(UTC),
                daily_submission_limit=arguments.daily_submission_limit,
            ).execute(
                source_opportunity_id=arguments.source_opportunity_id,
                risk_decision=decision,
                request=request,
                dispatch_deadline=datetime.now(UTC) + timedelta(seconds=10),
            )
            _write(
                arguments.evidence_path,
                {
                    "mode": "armed_one_shot",
                    "target": "paper",
                    "client_order_id": identity.client_order_id,
                    "intent_identity": decision.intent.intent_identity,
                    "halted_reason": outcome.halted_reason,
                    "submission": None
                    if outcome.submission is None
                    else outcome.submission.status.value,
                    "reconciled": outcome.reconciliation is not None,
                },
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
