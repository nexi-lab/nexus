"""OperationContext gains zone_set: tuple[str, ...] — allow-list (#3785)."""

from __future__ import annotations

from nexus.contracts.types import OperationContext


def test_zone_set_defaults_to_zone_id_singleton():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng")
    assert ctx.zone_set == ("eng",)


def test_zone_set_explicit_overrides_default():
    ctx = OperationContext(
        user_id="alice",
        groups=[],
        zone_id="eng",
        zone_set=("eng", "ops"),
    )
    assert ctx.zone_set == ("eng", "ops")


def test_zone_set_empty_when_zone_id_is_none():
    ctx = OperationContext(user_id="alice", groups=[], zone_id=None)
    assert ctx.zone_set == ()


def test_zone_set_is_tuple_for_hashability():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng", zone_set=("eng", "ops"))
    assert isinstance(ctx.zone_set, tuple)
