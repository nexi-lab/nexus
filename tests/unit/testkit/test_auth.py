from __future__ import annotations

from testkit.auth import (
    TEST_ADMIN_CONTEXT,
    TEST_CONTEXT,
    make_admin_context,
    make_context,
    make_zone_context,
)


def test_shared_contexts_are_reexported() -> None:
    assert TEST_CONTEXT.user_id == "test"
    assert TEST_CONTEXT.is_admin is False
    assert TEST_ADMIN_CONTEXT.user_id == "test-admin"
    assert TEST_ADMIN_CONTEXT.is_admin is True


def test_make_context_builds_user_context() -> None:
    ctx = make_context(user_id="alice", groups=["eng"], zone_id="zone-a")
    assert ctx.user_id == "alice"
    assert ctx.groups == ["eng"]
    assert ctx.zone_id == "zone-a"
    assert ctx.zone_set == ("zone-a",)
    assert ctx.zone_perms == (("zone-a", "rw"),)
    assert ctx.is_admin is False


def test_make_admin_context_sets_admin_flag() -> None:
    ctx = make_admin_context(user_id="root", groups=["ops"])
    assert ctx.user_id == "root"
    assert ctx.groups == ["ops"]
    assert ctx.is_admin is True


def test_make_zone_context_sets_zone_permissions() -> None:
    ctx = make_zone_context("zone-b", user_id="bob", perms="r")
    assert ctx.user_id == "bob"
    assert ctx.zone_id == "zone-b"
    assert ctx.zone_set == ("zone-b",)
    assert ctx.zone_perms == (("zone-b", "r"),)
