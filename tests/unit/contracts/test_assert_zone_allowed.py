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


# ── #3785 F3c: per-zone permission gating ──────────────────────


def test_required_perm_default_read_passes_on_rw():
    """Default required_perm='r' satisfied by 'rw' (back-compat path)."""
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng", zone_set=("eng",))
    assert_zone_allowed(ctx, "eng")  # no raise


def test_required_write_on_read_only_zone_raises():
    """eng:r token + required_perm='w' -> PermissionError with perm in message."""
    ctx = OperationContext(user_id="alice", groups=[], zone_perms=(("eng", "r"),))
    with pytest.raises(PermissionError) as exc:
        assert_zone_allowed(ctx, "eng", required_perm="w")
    msg = str(exc.value)
    assert "'eng'" in msg
    assert "'w'" in msg
    assert "'r'" in msg


def test_required_write_on_rw_zone_passes():
    """eng:rw token + required_perm='w' -> ok."""
    ctx = OperationContext(user_id="alice", groups=[], zone_perms=(("eng", "rw"),))
    assert_zone_allowed(ctx, "eng", required_perm="w")  # no raise


def test_x_perm_covers_anything():
    """eng:rwx token covers any required_perm (Unix 'x is god')."""
    ctx = OperationContext(user_id="alice", groups=[], zone_perms=(("eng", "rwx"),))
    assert_zone_allowed(ctx, "eng", required_perm="r")
    assert_zone_allowed(ctx, "eng", required_perm="w")
    # bogus perm — still passes thanks to "x in perms"
    assert_zone_allowed(ctx, "eng", required_perm="zzz")


def test_zone_not_in_allow_list_raises_with_perm_required():
    """Zone absent from allow-list -> PermissionError mentioning the allow-list."""
    ctx = OperationContext(user_id="alice", groups=[], zone_perms=(("eng", "rw"),))
    with pytest.raises(PermissionError) as exc:
        assert_zone_allowed(ctx, "ops", required_perm="w")
    msg = str(exc.value)
    assert "'ops'" in msg
    assert "allow-list" in msg
    assert "eng" in msg


def test_admin_bypasses_perm_check():
    """is_admin=True short-circuits even when required_perm wouldn't match."""
    ctx = OperationContext(
        user_id="root",
        groups=[],
        is_admin=True,
        zone_perms=(("eng", "r"),),
    )
    assert_zone_allowed(ctx, "eng", required_perm="w")  # no raise


def test_zone_perms_canonical_when_both_set():
    """zone_perms wins; zone_set is rebuilt from it."""
    ctx = OperationContext(
        user_id="alice",
        groups=[],
        zone_set=("ignored",),
        zone_perms=(("eng", "r"), ("ops", "rw")),
    )
    assert ctx.zone_set == ("eng", "ops")
    assert_zone_allowed(ctx, "ops", required_perm="w")
    with pytest.raises(PermissionError):
        assert_zone_allowed(ctx, "eng", required_perm="w")


def test_zone_set_only_derives_rw_perms():
    """Legacy: only zone_set passed -> zone_perms derived as ('rw',) for each zone."""
    ctx = OperationContext(user_id="alice", groups=[], zone_set=("eng",))
    assert ctx.zone_perms == (("eng", "rw"),)
    assert_zone_allowed(ctx, "eng", required_perm="w")  # no raise
