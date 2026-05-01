"""Tests for target-not-empty restore guard (#3793, Task 10)."""

import pytest

from nexus.bricks.archive.errors import ArchiveTargetNotEmpty
from nexus.bricks.portability.import_service import _check_target_empty


def test_empty_target_passes():
    """No zones present — guard must not raise."""
    _check_target_empty(existing_zones=[], force=False)


def test_non_empty_target_raises_without_force():
    """Zones present and force=False — must raise ArchiveTargetNotEmpty."""
    with pytest.raises(ArchiveTargetNotEmpty) as exc_info:
        _check_target_empty(existing_zones=["eng", "ops"], force=False)
    assert exc_info.value.existing_zones == ["eng", "ops"]


def test_non_empty_target_passes_with_force():
    """Zones present but force=True — guard must not raise."""
    _check_target_empty(existing_zones=["eng", "ops"], force=True)
