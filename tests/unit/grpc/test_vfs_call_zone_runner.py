from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from unittest.mock import MagicMock

import pytest

from nexus.grpc.servicer import VFSCallDispatcher
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

T = TypeVar("T")


class RecordingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        self.calls += 1
        return await work()


class RecordingRegistry:
    def __init__(self) -> None:
        self.zones: list[str] = []
        self.runner = RecordingRunner()

    def runner_for(self, zone_id: str) -> RecordingRunner:
        self.zones.append(zone_id)
        return self.runner


@pytest.mark.asyncio
async def test_grpc_call_dispatch_runs_in_target_zone_runner() -> None:
    registry = RecordingRegistry()

    async def echo(path: str, context: Any) -> dict[str, str]:
        return {"path": path, "zone": context.zone_id or "root"}

    dispatcher = VFSCallDispatcher(
        nexus_fs=MagicMock(),
        exposed_methods={"echo": echo},
        zone_registry=registry,
    )

    is_error, payload = await dispatcher._dispatch_async(
        "echo",
        encode_rpc_message({"path": "/zone/eng/docs/a.txt"}),
        {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "root",
            "zone_perms": [["eng", "rw"]],
            "is_admin": False,
        },
    )

    assert is_error is False
    decoded = decode_rpc_message(payload)
    assert decoded["result"]["path"] == "/zone/eng/docs/a.txt"
    assert decoded["result"]["zone"] == "eng"
    assert registry.zones == ["eng"]
    assert registry.runner.calls == 1


def test_dispatcher_preserves_legacy_positional_loop_argument() -> None:
    loop = asyncio.new_event_loop()
    try:
        dispatcher = VFSCallDispatcher(
            MagicMock(),
            {},
            None,
            None,
            None,
            loop,
        )

        assert dispatcher._loop is loop
        assert dispatcher._zone_registry is None
    finally:
        loop.close()
