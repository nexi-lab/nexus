"""Auth and identity fixtures for Nexus tests."""

from __future__ import annotations

from nexus.contracts.types import OperationContext
from tests.helpers.test_context import TEST_ADMIN_CONTEXT, TEST_CONTEXT

__all__ = [
    "TEST_ADMIN_CONTEXT",
    "TEST_CONTEXT",
    "make_admin_context",
    "make_context",
    "make_zone_context",
]


def make_context(
    *,
    user_id: str = "test",
    groups: list[str] | None = None,
    zone_id: str | None = None,
    zone_set: tuple[str, ...] = (),
    zone_perms: tuple[tuple[str, str], ...] = (),
    agent_id: str | None = None,
    subject_type: str = "user",
    subject_id: str | None = None,
    is_admin: bool = False,
    is_system: bool = False,
) -> OperationContext:
    """Build an `OperationContext` with explicit identity fields."""

    return OperationContext(
        user_id=user_id,
        groups=list(groups or []),
        zone_id=zone_id,
        zone_set=zone_set,
        zone_perms=zone_perms,
        agent_id=agent_id,
        subject_type=subject_type,
        subject_id=subject_id,
        is_admin=is_admin,
        is_system=is_system,
    )


def make_admin_context(
    *,
    user_id: str = "test-admin",
    groups: list[str] | None = None,
    zone_id: str | None = None,
) -> OperationContext:
    """Build an admin `OperationContext` for tests."""

    return make_context(
        user_id=user_id,
        groups=groups,
        zone_id=zone_id,
        is_admin=True,
    )


def make_zone_context(
    zone_id: str,
    *,
    user_id: str = "test",
    groups: list[str] | None = None,
    perms: str = "rw",
    is_admin: bool = False,
) -> OperationContext:
    """Build a context scoped to one zone with the provided permission string."""

    return make_context(
        user_id=user_id,
        groups=groups,
        zone_id=zone_id,
        zone_set=(zone_id,),
        zone_perms=((zone_id, perms),),
        is_admin=is_admin,
    )
