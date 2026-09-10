"""Tests for canonical dataset serialization and SHA-256 identity."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, Inexact, localcontext

from shadow.data import canonical_dataset_bytes, dataset_fingerprint
from tests.test_market_validation import _bars, _metadata

EXPECTED_VALID_FIXTURE_FINGERPRINT = (
    "b227516ad725425901ec316736e9ebed91705871b2d54f2d915154ae6cb48a2a"
)


def test_valid_fixture_has_a_stable_expected_fingerprint() -> None:
    bars = _bars("valid_bars.json")

    assert dataset_fingerprint(bars, _metadata()) == EXPECTED_VALID_FIXTURE_FINGERPRINT
    assert dataset_fingerprint(bars, _metadata()) == dataset_fingerprint(bars, _metadata())


def test_equivalent_decimal_representation_has_the_same_identity() -> None:
    bars = _bars("valid_bars.json")
    equivalent = replace(bars[0], open=Decimal("470.0"))

    assert dataset_fingerprint([equivalent, *bars[1:]], _metadata()) == dataset_fingerprint(
        bars, _metadata()
    )


def test_material_observation_change_changes_identity() -> None:
    bars = _bars("valid_bars.json")
    changed = [replace(bars[0], close=Decimal("470.21")), *bars[1:]]

    assert dataset_fingerprint(changed, _metadata()) != dataset_fingerprint(bars, _metadata())


def test_fingerprint_is_exact_and_independent_of_decimal_context() -> None:
    bars = _bars("valid_bars.json")
    changed = [replace(bars[0], close=Decimal("470.20000000000000000000000000001")), *bars[1:]]
    expected = dataset_fingerprint(changed, _metadata())
    assert expected != dataset_fingerprint(bars, _metadata())
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.traps[Inexact] = True
        assert dataset_fingerprint(changed, _metadata()) == expected


def test_appending_future_record_preserves_prior_canonical_record() -> None:
    bars = _bars("valid_bars.json")
    future = replace(
        bars[-1],
        observation_time=datetime(2024, 1, 2, 14, 34, tzinfo=UTC),
        availability_time=datetime(2024, 1, 2, 14, 34, 1, tzinfo=UTC),
    )
    expanded_metadata = replace(_metadata(), coverage_end=datetime(2024, 1, 2, 14, 34, tzinfo=UTC))

    original_records = json.loads(canonical_dataset_bytes(bars, _metadata()))["records"]
    expanded_document = json.loads(canonical_dataset_bytes([*bars, future], expanded_metadata))
    expanded_records = expanded_document["records"]

    assert expanded_records[: len(original_records)] == original_records
