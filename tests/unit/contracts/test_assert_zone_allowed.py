"""assert_zone_allowed — gate explicit zone references against token allow-list (#3785)."""

from __future__ import annotations

import pytest

from nexus.contracts.types import OperationContext, assert_zone_allowed


def test_in_set_passes():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng", zone_set=("eng", "ops"))
    assert_zone_allowed(ctx, "ops")  # no raise


def test_out_of_set_raises():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng", zone_set=("eng",))
    with pytest.raises(PermissionError) as exc:
        assert_zone_allowed(ctx, "legal")
    assert "legal" in str(exc.value)
    assert "('eng',)" in str(exc.value) or "['eng']" in str(exc.value)


def test_admin_bypasses_set():
    ctx = OperationContext(
        user_id="root", groups=[], zone_id="eng", is_admin=True, zone_set=("eng",)
    )
    assert_zone_allowed(ctx, "legal")  # no raise
