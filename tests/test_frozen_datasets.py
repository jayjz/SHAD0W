"""Adversarial checks for P1B's content-addressed P0.1 bar boundary."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

import pytest

from shadow.data import FrozenDatasetError, freeze_bars, load_frozen_bars
from tests.test_market_validation import _bars, _metadata


def test_freeze_is_deduplicated_and_context_independent(tmp_path: Path) -> None:
    bars, metadata = _bars("valid_bars.json"), _metadata()
    reference = freeze_bars(tmp_path, bars, metadata)
    assert freeze_bars(tmp_path, bars, metadata) == reference
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert freeze_bars(tmp_path, bars, metadata) == reference
    loaded = load_frozen_bars(tmp_path, reference)
    assert loaded.bars == tuple(bars)
    assert loaded.metadata.dataset_id == metadata.dataset_id


def test_freeze_identity_and_loading_fail_closed_on_tampering(tmp_path: Path) -> None:
    bars, metadata = _bars("valid_bars.json"), _metadata()
    reference = freeze_bars(tmp_path, bars, metadata)
    changed = freeze_bars(
        tmp_path, [replace(bars[0], close=Decimal("470.21")), *bars[1:]], metadata
    )
    assert changed.sha256 != reference.sha256
    path = tmp_path / "frozen-bars" / reference.sha256[:2] / f"{reference.sha256}.json"
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b"470.2", b"470.9", 1))
    with pytest.raises(FrozenDatasetError, match="digest"):
        load_frozen_bars(tmp_path, reference)


def test_freeze_rejects_wrong_path_and_metadata_disagreement(tmp_path: Path) -> None:
    reference = freeze_bars(tmp_path, _bars("valid_bars.json"), _metadata())
    wrong = replace(reference, dataset_id="other")
    with pytest.raises(FrozenDatasetError, match="metadata"):
        load_frozen_bars(tmp_path, wrong)
    path = tmp_path / "frozen-bars" / reference.sha256[:2] / f"{reference.sha256}.json"
    moved = path.with_name("0" * 64 + ".json")
    path.replace(moved)
    with pytest.raises(FrozenDatasetError, match="expected identity path"):
        load_frozen_bars(tmp_path, reference)
