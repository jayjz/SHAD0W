"""Packaging-only smoke tests."""

import shadow


def test_shadow_package_imports() -> None:
    """The bootstrap package is importable from the configured source layout."""
    assert shadow.__version__ == "0.0.0"
