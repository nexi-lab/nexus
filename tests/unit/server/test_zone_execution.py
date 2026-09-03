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


def test_context_for_target_zone_does_not_mutate_admin_context() -> None:
    """#4740: retargeting returns a copy; the caller's context keeps its zone/perms."""
    from nexus.contracts.types import OperationContext

    context = OperationContext(user_id="admin", groups=[], zone_id="root", is_admin=True)

    result = context_for_target_zone(context, "eng")

    assert result is not context
    assert context.zone_id == "root"
    assert context.zone_perms == (("root", "rw"),)
    assert result.zone_id == "eng"
    assert result.zone_perms == (("eng", "rw"),)
    assert result.zone_set == ("eng",)
    assert result.is_admin is True
    assert result.request_id == context.request_id


def test_context_for_target_zone_copies_non_dataclass_contexts() -> None:
    context = SimpleNamespace(
        zone_id="eng",
        zone_set=("eng", "ops"),
        zone_perms=(("eng", "r"), ("ops", "r")),
        is_admin=False,
    )

    result = context_for_target_zone(context, "ops")

    assert result is not context
    assert context.zone_id == "eng"
    assert result.zone_id == "ops"
    assert result.zone_perms == (("eng", "r"), ("ops", "r"))


def test_context_for_target_zone_returns_same_object_when_not_allowed() -> None:
    context = SimpleNamespace(zone_id="eng", zone_set=("eng",), zone_perms=(("eng", "rw"),))

    assert context_for_target_zone(context, "ops") is context
    assert context_for_target_zone(context, None) is context
    assert context_for_target_zone(context, "eng") is context
