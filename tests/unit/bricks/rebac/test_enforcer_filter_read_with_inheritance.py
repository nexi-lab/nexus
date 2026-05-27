"""PermissionEnforcer filter_list inheritance regressions."""

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


def test_filter_list_batches_parent_grants_in_primary_chain() -> None:
    """Normal filter_list() should not need search-specific fallback."""
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

    assert enforcer.filter_list(paths, ctx) == paths[:2]
    assert rebac.rebac_check_bulk.call_count == 1
    rebac.rebac_check.assert_not_called()
