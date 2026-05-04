"""Shared auth and operation context helpers for tests."""

from __future__ import annotations

from collections.abc import Iterable

from nexus.contracts.types import OperationContext

TEST_CONTEXT = OperationContext(
    user_id="test",
    groups=[],
    is_admin=False,
)

TEST_ADMIN_CONTEXT = OperationContext(
    user_id="test-admin",
    groups=[],
    is_admin=True,
)


def operation_context(
    *,
    user_id: str = "test",
    groups: Iterable[str] = (),
    zone_id: str | None = None,
    is_system: bool = False,
    is_admin: bool = False,
) -> OperationContext:
    """Build an OperationContext for tests with explicit identity fields."""
    return OperationContext(
        user_id=user_id,
        groups=list(groups),
        zone_id=zone_id,
        is_system=is_system,
        is_admin=is_admin,
    )
