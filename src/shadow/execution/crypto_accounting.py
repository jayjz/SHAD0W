"""Provider-neutral execution accounting. Coverage is evidence, never an estimate."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from shadow.execution.broker import BrokerFill, Evidence
from shadow.risk.models import OrderSide


def exact_decimal(value: Fraction) -> Decimal:
    """Convert finite decimal arithmetic without using the ambient context."""
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    if denominator != 1:
        raise ValueError("non-decimal inventory effect")
    places = max(twos, fives)
    integer = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    return Decimal((int(integer < 0), tuple(map(int, str(abs(integer)))), -places))


@dataclass(frozen=True, slots=True)
class CryptoFeeActivity:
    evidence: Evidence
    activity_id: str
    activity_type: str
    provider_date: date
    amount: Decimal
    asset: str | None
    execution_id: str | None
    symbol: str | None = None
    correction_of: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, Evidence) or type(self.provider_date) is not date:
            raise ValueError("typed fee evidence and provider date required")
        for value in (self.activity_id, self.activity_type):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("activity identity required")
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite() or self.amount < 0:
            raise ValueError("nonnegative fee required; reversals need explicit correction")
        for optional in (self.asset, self.execution_id, self.symbol, self.correction_of):
            if optional is not None and (not isinstance(optional, str) or not optional.strip()):
                raise ValueError("invalid optional activity identity")


@dataclass(frozen=True, slots=True)
class CryptoInventoryEffect:
    execution: BrokerFill
    fees: tuple[CryptoFeeActivity, ...]
    net_btc: Decimal
    usd_cash: Decimal


@dataclass(frozen=True, slots=True)
class CryptoActivityEvidence:
    """An explicit cut. Exhaustion alone proves neither retention nor fee finality.

    fee_complete_ids requires provider evidence of all fees (including zero) for
    each named execution. Alpaca's documented legacy REST rows cannot supply it.
    A date-only fee retains its date; evidence observation time is collection time,
    never a fabricated execution timestamp.
    """

    evidence: Evidence
    history_start: datetime
    history_end: datetime
    executions: tuple[BrokerFill, ...]
    fees: tuple[CryptoFeeActivity, ...]
    unsupported: tuple[str, ...]
    query_exhausted: bool
    history_verified: bool
    fee_complete_ids: tuple[str, ...]
    coverage_reference: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence, Evidence)
            or not isinstance(self.executions, tuple)
            or not all(isinstance(item, BrokerFill) for item in self.executions)
            or not isinstance(self.fees, tuple)
            or not all(isinstance(item, CryptoFeeActivity) for item in self.fees)
            or not isinstance(self.unsupported, tuple)
            or not all(isinstance(item, str) and item for item in self.unsupported)
            or not isinstance(self.fee_complete_ids, tuple)
            or not all(isinstance(item, str) and item for item in self.fee_complete_ids)
        ):
            raise ValueError("typed immutable activity evidence required")
        if (
            self.history_start.tzinfo is None
            or self.history_end.tzinfo is None
            or self.history_start > self.history_end
            or self.history_end > self.evidence.availability_time
        ):
            raise ValueError("invalid activity coverage")
        for value in (self.query_exhausted, self.history_verified):
            if type(value) is not bool:
                raise ValueError("explicit coverage flags required")
        if (self.history_verified or self.fee_complete_ids) and not self.coverage_reference:
            raise ValueError("provider coverage reference required")


def inventory_effects(batch: CryptoActivityEvidence) -> tuple[CryptoInventoryEffect, ...]:
    """Fail closed; callers distinguish missing coverage from contradictory facts."""
    if batch.unsupported:
        raise ValueError("unsupported broker activity")
    binding = (batch.evidence.account_id, batch.evidence.operational_scope)
    executions: dict[str, BrokerFill] = {}
    fees: dict[str, CryptoFeeActivity] = {}
    for fill in batch.executions:
        if (fill.evidence.account_id, fill.evidence.operational_scope) != binding:
            raise ValueError("execution binding conflict")
        if fill.instrument.identifier != "BTC/USD":
            raise ValueError("unlinked non-BTC execution")
        if not batch.history_start <= fill.evidence.observation_time <= batch.history_end:
            raise ValueError("execution outside coverage")
        if fill.evidence.availability_time > batch.evidence.availability_time:
            raise ValueError("future execution availability")
        if fill.execution_id in executions and executions[fill.execution_id] != fill:
            raise ValueError("conflicting duplicate execution")
        executions[fill.execution_id] = fill
    for fee in batch.fees:
        if (fee.evidence.account_id, fee.evidence.operational_scope) != binding:
            raise ValueError("fee binding conflict")
        if fee.evidence.availability_time > batch.evidence.availability_time:
            raise ValueError("future fee availability")
        if fee.activity_id in fees and fees[fee.activity_id] != fee:
            raise ValueError("conflicting duplicate fee")
        if fee.correction_of is not None:
            raise ValueError("fee correction requires recovery")
        if fee.asset not in ("BTC", "USD"):
            raise ValueError("unsupported or ambiguous fee asset")
        if fee.execution_id is not None and fee.execution_id not in executions:
            raise ValueError("unlinked broker fee")
        fees[fee.activity_id] = fee
    if fees.keys() & executions.keys():
        raise ValueError("activity identity reused across execution and fee")
    if set(batch.fee_complete_ids) - executions.keys():
        raise ValueError("fee coverage names unknown execution")
    if (
        not batch.query_exhausted
        or not batch.history_verified
        or set(batch.fee_complete_ids) != executions.keys()
        or any(fee.execution_id is None for fee in fees.values())
    ):
        raise LookupError("execution history or fee linkage/finality is unproven")
    effects = []
    for key, fill in sorted(executions.items()):
        linked = tuple(
            sorted(
                (fee for fee in fees.values() if fee.execution_id == key),
                key=lambda fee: fee.activity_id,
            )
        )
        btc_fee = sum((Fraction(fee.amount) for fee in linked if fee.asset == "BTC"), Fraction())
        usd_fee = sum((Fraction(fee.amount) for fee in linked if fee.asset == "USD"), Fraction())
        sign = 1 if fill.side is OrderSide.BUY else -1
        btc = sign * Fraction(fill.quantity) - btc_fee
        cash = -sign * Fraction(fill.quantity) * Fraction(fill.price) - usd_fee
        if fill.side is OrderSide.BUY and btc < 0:
            raise ValueError("fee exceeds received BTC")
        effects.append(CryptoInventoryEffect(fill, linked, exact_decimal(btc), exact_decimal(cash)))
    return tuple(effects)
