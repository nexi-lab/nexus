"""PermissionEnforcer search-filter batching regressions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.types import OperationContext

Check = tuple[tuple[str, str], str, tuple[str, str]]
AllowedKey = tuple[tuple[str, str], str, tuple[str, str], str]


def _make_mock_rebac(allowed_map: dict[AllowedKey, bool]) -> MagicMock:
    rebac = MagicMock()

    def _check(
        subject: tuple[str, str],
        permission: str,
        obj: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        key = (subject, permission, obj, zone_id or "root")
        return allowed_map.get(key, False)

    def _check_bulk(checks: list[Check], zone_id: str | None = None) -> dict[Check, bool]:
        return {check: _check(check[0], check[1], check[2], zone_id) for check in checks}

    rebac.rebac_check.side_effect = _check
    rebac.rebac_check_bulk.side_effect = _check_bulk
    return rebac


def test_filter_read_with_inheritance_batches_parent_grants() -> None:
    """Search fallback should recover inherited grants with one bulk check."""
    paths = [
        "/workspace/demo/herb/customers/cust-001.md",
        "/workspace/demo/herb/customers/cust-002.md",
        "/workspace/private/secret.md",
    ]
    rebac = _make_mock_rebac(
        {
            (
                ("user", "alice"),
                "read",
                ("file", "/workspace/demo/herb/customers"),
                "root",
            ): True,
        }
    )
    enforcer = PermissionEnforcer(rebac_manager=rebac)
    ctx = OperationContext(user_id="alice", groups=[])

    assert enforcer.filter_read_with_inheritance(paths, ctx) == paths[:2]
    assert rebac.rebac_check_bulk.call_count == 1
    rebac.rebac_check.assert_not_called()


def test_filter_read_with_inheritance_honors_zone_perms_allowlist() -> None:
    paths = [
        "/zone/eng/visible.md",
        "/zone/ops/hidden.md",
    ]
    rebac = _make_mock_rebac(
        {
            (("user", "alice"), "read", ("file", "/eng"), "root"): True,
            (("user", "alice"), "read", ("file", "/ops"), "root"): True,
        }
    )
    enforcer = PermissionEnforcer(rebac_manager=rebac)
    ctx = OperationContext(
        user_id="alice",
        groups=[],
        zone_id="root",
        zone_perms=(("eng", "r"),),
    )

    assert enforcer.filter_read_with_inheritance(paths, ctx) == ["/zone/eng/visible.md"]
    assert rebac.rebac_check_bulk.call_count == 0
