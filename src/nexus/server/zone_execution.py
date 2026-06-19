from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

T = TypeVar("T")


def context_allows_target_zone(context: Any, target_zone: str) -> bool:
    if getattr(context, "is_admin", False):
        return True
    if getattr(context, "zone_id", None) == target_zone:
        return True
    if target_zone in set(getattr(context, "zone_set", ()) or ()):
        return True
    for zone_perm in getattr(context, "zone_perms", ()) or ():
        if isinstance(zone_perm, (list, tuple)) and zone_perm and zone_perm[0] == target_zone:
            return True
    return False


def context_for_target_zone(context: Any, target_zone: str | None) -> Any:
    if target_zone is None or getattr(context, "zone_id", None) == target_zone:
        return context
    if not context_allows_target_zone(context, target_zone):
        return context
    zone_perms = tuple(getattr(context, "zone_perms", ()) or ())
    real_zone_perms = tuple(
        zp for zp in zone_perms if isinstance(zp, (list, tuple)) and zp and zp[0] != "root"
    )
    if len(real_zone_perms) > 1 and getattr(context, "zone_id", None) == "root":
        return context
    updates: dict[str, Any] = {"zone_id": target_zone}
    if getattr(context, "is_admin", False):
        updates["zone_set"] = (target_zone,)
        updates["zone_perms"] = ((target_zone, "rw"),)
    for key, value in updates.items():
        setattr(context, key, value)
    return context


async def run_zone_scoped(
    zone_registry: Any | None,
    zone_id: str | None,
    work: Callable[[], Awaitable[T]],
) -> T:
    if zone_registry is None or zone_id is None:
        return await work()
    return cast(T, await zone_registry.runner_for(zone_id).call(work))
