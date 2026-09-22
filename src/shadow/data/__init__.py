"""Market-data validation and deterministic dataset identity."""

from shadow.data.fingerprint import canonical_dataset_bytes, dataset_fingerprint
from shadow.data.frozen import (
    FrozenBarDataset,
    FrozenBarDatasetRef,
    FrozenDatasetError,
    freeze_bars,
    load_frozen_bars,
)
from shadow.data.validation import DatasetValidationReport, validate_bars, validate_quotes

__all__ = [
    "DatasetValidationReport",
    "FrozenBarDataset",
    "FrozenBarDatasetRef",
    "FrozenDatasetError",
    "canonical_dataset_bytes",
    "dataset_fingerprint",
    "freeze_bars",
    "load_frozen_bars",
    "validate_bars",
    "validate_quotes",
]
