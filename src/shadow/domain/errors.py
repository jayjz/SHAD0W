"""Errors raised when market-data contract invariants are violated."""

from __future__ import annotations


class MarketDataValidationError(ValueError):
    """Identifies a violated market-data invariant and, when known, its record."""

    def __init__(self, invariant: str, detail: str, record_index: int | None = None) -> None:
        self.invariant = invariant
        self.record_index = record_index
        location = "" if record_index is None else f" at record {record_index}"
        super().__init__(f"{invariant}{location}: {detail}")
