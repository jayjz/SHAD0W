"""Provider-neutral market-data domain contracts."""

from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    DatasetMetadata,
    Instrument,
    Provenance,
    Quote,
    QuoteMarketState,
    ValidationStatus,
)

__all__ = [
    "AvailabilitySemantics",
    "Bar",
    "BarInterval",
    "DatasetMetadata",
    "Instrument",
    "MarketDataValidationError",
    "Provenance",
    "Quote",
    "QuoteMarketState",
    "ValidationStatus",
]
