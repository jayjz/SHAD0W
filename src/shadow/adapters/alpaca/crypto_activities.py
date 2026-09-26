"""Bounded legacy Trading API reads; no undocumented fee-link inference."""

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from shadow.domain.market import Instrument
from shadow.execution.broker import BrokerFill, Evidence
from shadow.execution.crypto_accounting import CryptoActivityEvidence, CryptoFeeActivity
from shadow.risk.models import OrderSide


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("activity text missing/malformed")
    return value


def _amount(value: object) -> Decimal:
    try:
        result = Decimal(_text(value))
    except InvalidOperation as exc:
        raise ValueError("malformed activity amount") from exc
    if not result.is_finite():
        raise ValueError("nonfinite activity amount")
    return result


def translate_activities(
    rows: tuple[dict[str, object], ...],
    *,
    evidence: Evidence,
    history_start: datetime,
    history_end: datetime,
) -> CryptoActivityEvidence:
    fills = []
    fees = []
    unsupported = []
    symbol: str | None
    for row in rows:
        identity = _text(row.get("id"))
        kind = _text(row.get("activity_type"))
        if any(row.get(key) is not None for key in ("previous_id", "correction_of")):
            unsupported.append(f"{identity}:correction")
            continue
        if kind == "FILL":
            if row.get("type") not in ("fill", "partial_fill"):
                unsupported.append(f"{identity}:unsupported-fill-type")
                continue
            symbol = _text(row.get("symbol"))
            if symbol not in ("BTC/USD", "BTCUSD"):
                unsupported.append(f"{identity}:non-BTC-fill")
                continue
            stamp = datetime.fromisoformat(
                _text(row.get("transaction_time")).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None or not history_start <= stamp <= history_end:
                raise ValueError("activity execution escaped requested window")
            quantity = _amount(row.get("qty"))
            cumulative = _amount(row.get("cum_qty"))
            leaves = _amount(row.get("leaves_qty"))
            if quantity <= 0 or cumulative < quantity or leaves < 0:
                raise ValueError("invalid fill quantities")
            fills.append(
                BrokerFill(
                    Evidence(
                        identity,
                        evidence.account_id,
                        evidence.operational_scope,
                        stamp,
                        evidence.availability_time,
                    ),
                    identity,
                    _text(row.get("order_id")),
                    Instrument("BTC/USD"),
                    OrderSide(_text(row.get("side"))),
                    quantity,
                    _amount(row.get("price")),
                )
            )
        elif kind in ("CFEE", "FEE"):
            provider_date = date.fromisoformat(_text(row.get("date")))
            if not history_start.date() <= provider_date <= history_end.date():
                raise ValueError("fee escaped requested date window")
            net = _amount(row.get("net_amount"))
            if kind == "CFEE":
                quantity = _amount(row.get("qty"))
                if net != 0 or quantity > 0 or row.get("status") != "executed":
                    raise ValueError("unsupported crypto fee/reversal")
                amount, asset = quantity.copy_negate(), None
                symbol = _text(row.get("symbol"))
            else:
                if net > 0:
                    raise ValueError("fee reversal requires recovery")
                amount, asset = net.copy_negate(), "USD"
                symbol = None if row.get("symbol") is None else _text(row["symbol"])
            # Legacy documentation supplies no execution/order linkage or explicit
            # CFEE asset. Neither description nor same-date symbol is a safe join.
            fees.append(
                CryptoFeeActivity(
                    evidence,
                    identity,
                    kind,
                    provider_date,
                    amount,
                    asset,
                    None,
                    symbol,
                )
            )
        else:
            unsupported.append(f"{identity}:{kind}")
    return CryptoActivityEvidence(
        evidence,
        history_start,
        history_end,
        tuple(fills),
        tuple(fees),
        tuple(unsupported),
        True,
        False,
        (),
        None,
    )


def collect_activity_rows(
    read: Callable[[str], tuple[bytes, datetime]],
    *,
    history_start: datetime,
    history_end: datetime,
    max_pages: int,
) -> tuple[tuple[dict[str, object], ...], datetime]:
    if (
        history_start.tzinfo is None
        or history_end.tzinfo is None
        or history_start > history_end
        or type(max_pages) is not int
        or max_pages <= 0
    ):
        raise ValueError("explicit bounded activity window required")
    # NTA date precision differs from FILL timestamps; expand query bounds to
    # whole dates, then bound ordinary fills by time and fees by provider date.
    query = {
        "after": (history_start.date() - timedelta(days=1)).isoformat(),
        "until": (history_end.date() + timedelta(days=1)).isoformat(),
        "direction": "asc",
        "page_size": "100",
    }
    rows: dict[str, dict[str, object]] = {}
    cursors: set[str] = set()
    for _ in range(max_pages):
        body, received = read("/v2/account/activities?" + urlencode(query))
        page = json.loads(body)
        if not isinstance(page, list) or len(page) > 100:
            raise ValueError("malformed activity page")
        for row in page:
            if not isinstance(row, dict):
                raise ValueError("malformed activity row")
            identity = _text(row.get("id"))
            _text(row.get("activity_type"))
            if identity in rows and rows[identity] != row:
                raise ValueError("conflicting duplicate activity")
            rows[identity] = row
        if len(page) < 100:
            bounded = []
            for row in rows.values():
                # Keep translation strict, but remove ordinary fills/fees that
                # the deliberately date-widened provider query may return.
                # Corrections and unsupported fills retain their existing
                # translation behavior and are not reinterpreted here.
                if (
                    row.get("activity_type") == "FILL"
                    and row.get("type") in ("fill", "partial_fill")
                    and row.get("symbol") in ("BTC/USD", "BTCUSD")
                    and row.get("previous_id") is None
                    and row.get("correction_of") is None
                ):
                    try:
                        stamp = datetime.fromisoformat(
                            _text(row.get("transaction_time")).replace("Z", "+00:00")
                        )
                    except ValueError as exc:
                        raise ValueError("malformed activity execution timestamp") from exc
                    if stamp.tzinfo is None or stamp.utcoffset() is None:
                        raise ValueError("malformed activity execution timestamp")
                    if not history_start <= stamp <= history_end:
                        continue
                elif (
                    row.get("activity_type") in ("CFEE", "FEE")
                    and row.get("previous_id") is None
                    and row.get("correction_of") is None
                ):
                    provider_date = date.fromisoformat(_text(row.get("date")))
                    if not history_start.date() <= provider_date <= history_end.date():
                        continue
                bounded.append(row)
            return tuple(bounded), received
        cursor = _text(page[-1].get("id"))
        if cursor in cursors:
            raise ValueError("stalled activity pagination")
        cursors.add(cursor)
        query["page_token"] = cursor
    raise ValueError("activity pagination budget exhausted; history truncated")
