"""auth_bridge propagates zone_set through to OperationContext (#3785)."""

from __future__ import annotations

from nexus.bricks.mcp.auth_bridge import op_context_to_auth_dict
from nexus.contracts.types import OperationContext


def test_op_context_to_auth_dict_includes_zone_set():
    ctx = OperationContext(
        user_id="alice",
        groups=[],
        zone_id="eng",
        zone_set=("eng", "ops"),
        is_admin=False,
    )
    auth = op_context_to_auth_dict(ctx)
    assert auth["zone_id"] == "eng"
    assert auth["zone_set"] == ["eng", "ops"]
    assert auth["is_admin"] is False


def test_op_context_to_auth_dict_zone_set_defaults_to_zone_id_singleton():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng")
    auth = op_context_to_auth_dict(ctx)
    assert auth["zone_set"] == ["eng"]


def test_op_context_to_auth_dict_none_returns_anonymous_with_empty_zone_set():
    auth = op_context_to_auth_dict(None)
    assert auth["zone_set"] == []
