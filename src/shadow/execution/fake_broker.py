"""Scripted test support, separate from immutable production-facing contracts.

Every call consumes exactly one supplied step, including failures. No fabricated
account, clock, inventory, fills, retries, or successful default responses exist.
Malformed normalized evidence is injected as BrokerError(MALFORMED), not raw JSON.
"""

from dataclasses import dataclass
from typing import TypeVar

from shadow.domain import Instrument
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerContractError,
    BrokerError,
    BrokerOrder,
    BrokerSnapshot,
    SubmissionResult,
    SubmitRequest,
    TradeUpdate,
)


@dataclass(frozen=True, slots=True)
class BrokerCall:
    operation: str
    instrument: Instrument | None = None
    order_id: str | None = None
    client_id: str | None = None
    request: SubmitRequest | None = None


type BrokerReply = (
    BrokerAccount
    | BrokerClock
    | BrokerAsset
    | BrokerSnapshot
    | BrokerOrder
    | tuple[TradeUpdate, ...]
    | SubmissionResult
    | BrokerError
)


@dataclass(frozen=True, slots=True)
class BrokerStep:
    call: BrokerCall
    reply: BrokerReply


T = TypeVar("T")


class FakeBroker:
    def __init__(self, steps: tuple[BrokerStep, ...]) -> None:
        self._steps = tuple(steps)
        self._calls: list[BrokerCall] = []

    @property
    def calls(self) -> tuple[BrokerCall, ...]:
        return tuple(self._calls)

    def _next(self, call: BrokerCall) -> BrokerReply:
        index = len(self._calls)
        self._calls.append(call)
        if index >= len(self._steps):
            raise BrokerContractError("fake script exhausted")
        step = self._steps[index]
        if step.call != call:
            raise BrokerContractError("unexpected fake call")
        return step.reply

    def _read(self, call: BrokerCall, expected: type[T]) -> T | BrokerError:
        reply = self._next(call)
        if isinstance(reply, (expected, BrokerError)):
            return reply
        raise BrokerContractError("script reply violates broker port")

    def read_account(self) -> BrokerAccount | BrokerError:
        return self._read(BrokerCall("read_account"), BrokerAccount)

    def read_clock(self) -> BrokerClock | BrokerError:
        return self._read(BrokerCall("read_clock"), BrokerClock)

    def read_asset(self, instrument: Instrument) -> BrokerAsset | BrokerError:
        if not isinstance(instrument, Instrument):
            raise BrokerContractError("expected Instrument")
        return self._read(BrokerCall("read_asset", instrument=instrument), BrokerAsset)

    def read_snapshot(self) -> BrokerSnapshot | BrokerError:
        return self._read(BrokerCall("read_snapshot"), BrokerSnapshot)

    def lookup_order(
        self, *, order_id: str | None, client_id: str | None
    ) -> BrokerOrder | BrokerError:
        if (order_id is None) == (client_id is None):
            raise BrokerContractError("exactly one lookup identity required")
        identity = order_id if order_id is not None else client_id
        if not isinstance(identity, str) or not identity or identity != identity.strip():
            raise BrokerContractError("invalid lookup identity")
        return self._read(
            BrokerCall("lookup_order", order_id=order_id, client_id=client_id), BrokerOrder
        )

    def read_updates(self) -> tuple[TradeUpdate, ...] | BrokerError:
        reply = self._next(BrokerCall("read_updates"))
        if isinstance(reply, BrokerError):
            return reply
        if isinstance(reply, tuple) and all(isinstance(item, TradeUpdate) for item in reply):
            return reply
        raise BrokerContractError("script reply violates update port")

    def submit(self, request: SubmitRequest) -> SubmissionResult:
        if not isinstance(request, SubmitRequest):
            raise BrokerContractError("expected SubmitRequest, not an authorization")
        reply = self._next(BrokerCall("submit", request=request))
        if not isinstance(reply, SubmissionResult) or reply.request != request:
            raise BrokerContractError("script must supply a matching explicit submission outcome")
        return reply
