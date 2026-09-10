"""Immutable execution evidence and deterministic quote-side fill semantics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from shadow.domain import Instrument, Quote, QuoteMarketState

EXECUTION_MODEL_ID = "shadow.execution.quote_bid_ask.v1"


class ExecutionContractError(ValueError):
    """An invalid execution contract or unsupported execution-model input."""


class ExecutionActionType(StrEnum):
    """The lifecycle intent vocabulary supported by the initial model."""

    ENTRY = "entry"
    EXIT = "exit"


class ExecutionSide(StrEnum):
    """The market side selected for a modeled execution."""

    BUY = "buy"
    SELL = "sell"


class ExecutionStatus(StrEnum):
    """The only deterministic outcomes emitted for one execution attempt."""

    FILLED = "filled"
    UNFILLED = "unfilled"
    REJECTED = "rejected"


class ExecutionReason(StrEnum):
    """Machine-readable explanation for an execution outcome."""

    FILLED_AT_QUOTE = "filled_at_quote"
    NO_LEGAL_QUOTE = "no_legal_quote"
    QUOTE_NOT_YET_AVAILABLE = "quote_not_yet_available"
    STALE_QUOTE = "stale_quote"
    CROSSED_QUOTE = "crossed_quote"
    DUPLICATE_ATTEMPT_ID = "duplicate_attempt_id"
    ACTION_OPPORTUNITY_INSTRUMENT_MISMATCH = "action_opportunity_instrument_mismatch"
    ATTEMPT_TIME_DOES_NOT_EQUAL_OPPORTUNITY_TIME = "attempt_time_does_not_equal_opportunity_time"
    OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY = (
        "opportunity_not_strictly_after_eligibility_boundary"
    )
    EXECUTION_MODEL_MISMATCH = "execution_model_mismatch"


_UNFILLED_REASONS = frozenset(
    {
        ExecutionReason.NO_LEGAL_QUOTE,
        ExecutionReason.QUOTE_NOT_YET_AVAILABLE,
        ExecutionReason.STALE_QUOTE,
    }
)
_REJECTED_REASONS = frozenset(
    {
        ExecutionReason.CROSSED_QUOTE,
        ExecutionReason.DUPLICATE_ATTEMPT_ID,
        ExecutionReason.ACTION_OPPORTUNITY_INSTRUMENT_MISMATCH,
        ExecutionReason.ATTEMPT_TIME_DOES_NOT_EQUAL_OPPORTUNITY_TIME,
        ExecutionReason.OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY,
        ExecutionReason.EXECUTION_MODEL_MISMATCH,
    }
)


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty_trimmed(value: str, field_name: str) -> None:
    if not value or value != value.strip():
        raise ExecutionContractError(f"{field_name} must be a non-empty trimmed string")


def quote_reference(quote: Quote) -> str:
    """Return a stable reference for one provider-neutral quote value.

    P0.1 has no provider quote identifier.  This reference therefore identifies
    the complete immutable quote evidence rather than caller position or object
    identity, and is also the final deterministic tie-breaker for equal times.
    """
    if not isinstance(quote, Quote):
        raise ExecutionContractError("quote must be a Quote")
    provenance = quote.provenance
    return ":".join(
        (
            "quote",
            quote.instrument.identifier,
            quote.observation_time.isoformat(),
            quote.availability_time.isoformat(),
            quote.bid_price.to_eng_string(),
            quote.ask_price.to_eng_string(),
            "" if quote.bid_size is None else quote.bid_size.to_eng_string(),
            "" if quote.ask_size is None else quote.ask_size.to_eng_string(),
            quote.availability_semantics.value,
            provenance.source,
            provenance.source_timezone or "",
            provenance.session or "",
        )
    )


@dataclass(frozen=True, slots=True)
class QuoteExecutionConfig:
    """Explicit, immutable assumptions for the quote-side baseline.

    Freshness measures market age as ``attempt_time - quote.observation_time``.
    Availability is a separate causal requirement and is enforced independently.
    """

    maximum_quote_age: timedelta
    execution_model_id: str = EXECUTION_MODEL_ID

    def __post_init__(self) -> None:
        if not isinstance(self.maximum_quote_age, timedelta):
            raise ExecutionContractError("maximum_quote_age must be a timedelta")
        if self.maximum_quote_age < timedelta(0):
            raise ExecutionContractError("maximum_quote_age must not be negative")
        if self.execution_model_id != EXECUTION_MODEL_ID:
            raise ExecutionContractError(
                f"execution_model_id must be {EXECUTION_MODEL_ID!r} for QuoteExecutionConfig"
            )


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """One lifecycle action's attempt at one chronologically eligible opportunity."""

    attempt_id: str
    action_id: str
    signal_reference: str
    instrument: Instrument
    action_type: ExecutionActionType
    eligibility_after_time: datetime
    opportunity_reference: str
    opportunity_instrument: Instrument
    opportunity_time: datetime
    attempt_time: datetime
    execution_model_id: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("attempt_id", self.attempt_id),
            ("action_id", self.action_id),
            ("signal_reference", self.signal_reference),
            ("opportunity_reference", self.opportunity_reference),
            ("execution_model_id", self.execution_model_id),
        ):
            _nonempty_trimmed(value, field_name)
        if not isinstance(self.instrument, Instrument):
            raise ExecutionContractError("instrument must be an Instrument")
        if not isinstance(self.opportunity_instrument, Instrument):
            raise ExecutionContractError("opportunity_instrument must be an Instrument")
        if not isinstance(self.action_type, ExecutionActionType):
            raise ExecutionContractError("action_type must be an ExecutionActionType")
        for field_name in ("eligibility_after_time", "opportunity_time", "attempt_time"):
            object.__setattr__(
                self, field_name, _canonical_utc(getattr(self, field_name), field_name)
            )

    @property
    def execution_side(self) -> ExecutionSide:
        """Map the initial long-only lifecycle action to its executable side."""
        return (
            ExecutionSide.BUY
            if self.action_type is ExecutionActionType.ENTRY
            else ExecutionSide.SELL
        )

    @property
    def sort_key(
        self,
    ) -> tuple[datetime, datetime, datetime, str, str, str, str, str, str, str]:
        """Canonical key independent of caller ordering for batch resolution."""
        return (
            self.attempt_time,
            self.opportunity_time,
            self.eligibility_after_time,
            self.attempt_id,
            self.action_id,
            self.signal_reference,
            self.opportunity_reference,
            self.instrument.identifier,
            self.opportunity_instrument.identifier,
            self.action_type.value,
        )


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """The immutable deterministic result of one execution attempt."""

    attempt: ExecutionAttempt
    status: ExecutionStatus
    reason: ExecutionReason
    market_evidence: Quote | None
    execution_price: Decimal | None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, ExecutionAttempt):
            raise ExecutionContractError("attempt must be an ExecutionAttempt")
        if not isinstance(self.status, ExecutionStatus):
            raise ExecutionContractError("status must be an ExecutionStatus")
        if not isinstance(self.reason, ExecutionReason):
            raise ExecutionContractError("reason must be an ExecutionReason")
        if self.market_evidence is not None:
            if not isinstance(self.market_evidence, Quote):
                raise ExecutionContractError("market_evidence must be a Quote or None")
            if self.market_evidence.instrument != self.attempt.instrument:
                raise ExecutionContractError(
                    "market_evidence instrument must match attempt instrument"
                )
        if self.execution_price is not None:
            if (
                not isinstance(self.execution_price, Decimal)
                or not self.execution_price.is_finite()
            ):
                raise ExecutionContractError("execution_price must be a finite Decimal or None")

        if self.status is ExecutionStatus.FILLED:
            if (
                self.reason is not ExecutionReason.FILLED_AT_QUOTE
                or self.market_evidence is None
                or self.execution_price is None
            ):
                raise ExecutionContractError(
                    "filled outcome requires quote evidence, execution price, and filled reason"
                )
            if self.market_evidence.market_state is QuoteMarketState.CROSSED:
                raise ExecutionContractError("filled outcome cannot use crossed quote evidence")
            expected_price = (
                self.market_evidence.ask_price
                if self.attempt.execution_side is ExecutionSide.BUY
                else self.market_evidence.bid_price
            )
            if self.execution_price != expected_price:
                raise ExecutionContractError(
                    "filled outcome execution_price must match the executable quote side"
                )
        elif self.status is ExecutionStatus.UNFILLED:
            if self.reason not in _UNFILLED_REASONS or self.execution_price is not None:
                raise ExecutionContractError("unfilled outcome has incompatible reason or price")
        elif self.reason not in _REJECTED_REASONS or self.execution_price is not None:
            raise ExecutionContractError("rejected outcome has incompatible reason or price")

    @property
    def market_evidence_reference(self) -> str | None:
        """Return the stable reference to evidence actually evaluated for this outcome."""
        if self.market_evidence is None:
            return None
        return quote_reference(self.market_evidence)


def _quote_sort_key(quote: Quote) -> tuple[datetime, datetime, str]:
    """Choose newest legal availability, then newest observation, then stable evidence."""
    return (quote.availability_time, quote.observation_time, quote_reference(quote))


def _outcome(
    attempt: ExecutionAttempt,
    status: ExecutionStatus,
    reason: ExecutionReason,
    *,
    market_evidence: Quote | None = None,
    execution_price: Decimal | None = None,
) -> ExecutionOutcome:
    return ExecutionOutcome(
        attempt=attempt,
        status=status,
        reason=reason,
        market_evidence=market_evidence,
        execution_price=execution_price,
    )


def _attempt_rejection(
    attempt: ExecutionAttempt, config: QuoteExecutionConfig
) -> ExecutionReason | None:
    if attempt.execution_model_id != config.execution_model_id:
        return ExecutionReason.EXECUTION_MODEL_MISMATCH
    if attempt.instrument != attempt.opportunity_instrument:
        return ExecutionReason.ACTION_OPPORTUNITY_INSTRUMENT_MISMATCH
    if attempt.attempt_time != attempt.opportunity_time:
        return ExecutionReason.ATTEMPT_TIME_DOES_NOT_EQUAL_OPPORTUNITY_TIME
    if attempt.opportunity_time <= attempt.eligibility_after_time:
        return ExecutionReason.OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY
    return None


def resolve_execution_attempt(
    attempt: ExecutionAttempt,
    quotes: Iterable[Quote],
    config: QuoteExecutionConfig,
) -> ExecutionOutcome:
    """Resolve one attempt against only causally legal quote evidence.

    A legal quote must match the instrument and have become available no later than
    the attempt.  The newest legal quote wins deterministically; a newer crossed or
    stale quote fails closed rather than causing selection of an older, favorable one.
    """
    if not isinstance(attempt, ExecutionAttempt):
        raise ExecutionContractError("attempt must be an ExecutionAttempt")
    if not isinstance(config, QuoteExecutionConfig):
        raise ExecutionContractError("config must be a QuoteExecutionConfig")
    materialized_quotes = tuple(quotes)
    if not all(isinstance(quote, Quote) for quote in materialized_quotes):
        raise ExecutionContractError("quotes must contain only Quote values")

    rejection = _attempt_rejection(attempt, config)
    if rejection is not None:
        return _outcome(attempt, ExecutionStatus.REJECTED, rejection)

    matching_quotes = tuple(
        quote for quote in materialized_quotes if quote.instrument == attempt.instrument
    )
    legal_quotes = tuple(
        quote for quote in matching_quotes if quote.availability_time <= attempt.attempt_time
    )
    if not legal_quotes:
        reason = (
            ExecutionReason.QUOTE_NOT_YET_AVAILABLE
            if any(quote.availability_time > attempt.attempt_time for quote in matching_quotes)
            else ExecutionReason.NO_LEGAL_QUOTE
        )
        return _outcome(attempt, ExecutionStatus.UNFILLED, reason)

    selected = max(legal_quotes, key=_quote_sort_key)
    if selected.market_state is QuoteMarketState.CROSSED:
        return _outcome(
            attempt,
            ExecutionStatus.REJECTED,
            ExecutionReason.CROSSED_QUOTE,
            market_evidence=selected,
        )
    if attempt.attempt_time - selected.observation_time > config.maximum_quote_age:
        return _outcome(
            attempt,
            ExecutionStatus.UNFILLED,
            ExecutionReason.STALE_QUOTE,
            market_evidence=selected,
        )

    execution_price = (
        selected.ask_price if attempt.execution_side is ExecutionSide.BUY else selected.bid_price
    )
    return _outcome(
        attempt,
        ExecutionStatus.FILLED,
        ExecutionReason.FILLED_AT_QUOTE,
        market_evidence=selected,
        execution_price=execution_price,
    )


def resolve_execution_attempts(
    attempts: Iterable[ExecutionAttempt],
    quotes: Iterable[Quote],
    config: QuoteExecutionConfig,
) -> tuple[ExecutionOutcome, ...]:
    """Resolve a batch canonically, rejecting duplicate external attempt identifiers."""
    materialized_attempts = tuple(attempts)
    if not all(isinstance(attempt, ExecutionAttempt) for attempt in materialized_attempts):
        raise ExecutionContractError("attempts must contain only ExecutionAttempt values")
    materialized_quotes = tuple(quotes)
    if not all(isinstance(quote, Quote) for quote in materialized_quotes):
        raise ExecutionContractError("quotes must contain only Quote values")
    if not isinstance(config, QuoteExecutionConfig):
        raise ExecutionContractError("config must be a QuoteExecutionConfig")

    identifiers = [attempt.attempt_id for attempt in materialized_attempts]
    duplicate_ids = {identifier for identifier in identifiers if identifiers.count(identifier) > 1}
    outcomes: list[ExecutionOutcome] = []
    for attempt in sorted(materialized_attempts, key=lambda item: item.sort_key):
        if attempt.attempt_id in duplicate_ids:
            outcomes.append(
                _outcome(
                    attempt,
                    ExecutionStatus.REJECTED,
                    ExecutionReason.DUPLICATE_ATTEMPT_ID,
                )
            )
        else:
            outcomes.append(resolve_execution_attempt(attempt, materialized_quotes, config))
    return tuple(outcomes)
