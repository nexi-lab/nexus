from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import TypeVar

import pytest

from nexus.server.zone_execution import context_for_target_zone, run_zone_scoped

T = TypeVar("T")


class RecordingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        self.calls += 1
        return await work()


class RecordingRegistry:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.runner = RecordingRunner()

    def runner_for(self, zone_id: str) -> RecordingRunner:
        self.requested.append(zone_id)
        return self.runner


@pytest.mark.asyncio
async def test_run_zone_scoped_uses_runner_for_concrete_zone() -> None:
    registry = RecordingRegistry()

    async def work() -> str:
        return "ok"

    result = await run_zone_scoped(registry, "eng", work)

    assert result == "ok"
    assert registry.requested == ["eng"]
    assert registry.runner.calls == 1


@pytest.mark.asyncio
async def test_run_zone_scoped_runs_inline_without_registry() -> None:
    async def work() -> str:
        return "inline"

    assert await run_zone_scoped(None, "eng", work) == "inline"


@pytest.mark.asyncio
async def test_run_zone_scoped_runs_inline_without_target_zone() -> None:
    registry = RecordingRegistry()

    async def work() -> str:
        return "global"

    assert await run_zone_scoped(registry, None, work) == "global"
    assert registry.requested == []


def test_context_for_target_zone_preserves_root_for_multizone_token() -> None:
    context = SimpleNamespace(
        zone_id="root",
        zone_set=("company", "shared"),
        zone_perms=(("company", "r"), ("shared", "rw")),
        is_admin=False,
    )

    result = context_for_target_zone(context, "shared")

    assert result is context
    assert context.zone_id == "root"
    assert context.zone_perms == (("company", "r"), ("shared", "rw"))
