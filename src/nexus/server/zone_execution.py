from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

T = TypeVar("T")


async def run_zone_scoped(
    zone_registry: Any | None,
    zone_id: str | None,
    work: Callable[[], Awaitable[T]],
) -> T:
    if zone_registry is None or zone_id is None:
        return await work()
    return cast(T, await zone_registry.runner_for(zone_id).call(work))
