from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.lib.rpc_codec import encode_rpc_message
from nexus.server.api.core.rpc import router
from nexus.server.dependencies import require_auth

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


def test_http_rpc_auto_dispatch_runs_in_target_zone_runner() -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.nexus_fs = MagicMock()
    app.state.auth_provider = None
    app.state.subscription_manager = None
    app.state.zone_registry = registry

    async def echo(path: str, context: Any) -> dict[str, str]:
        return {"path": path, "zone": context.zone_id or "root"}

    app.state.exposed_methods = {"echo": echo}
    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "zone_id": "root",
        "zone_perms": [["eng", "rw"]],
        "is_admin": False,
    }
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post(
            "/api/nfs/echo",
            content=encode_rpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "echo",
                    "params": {"path": "/zone/eng/docs/a.txt"},
                }
            ),
        )

    assert response.status_code == 200
    assert registry.zones == ["eng"]
    assert registry.runner.calls == 1


def test_http_rpc_kernel_copy_runs_in_target_zone_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.nexus_fs = MagicMock()
    app.state.auth_provider = None
    app.state.subscription_manager = None
    app.state.zone_registry = registry
    app.state.operation_timeout = 30.0
    app.state.exposed_methods = {}

    async def fake_dispatch_kernel_syscall(
        nexus_fs: Any,
        method: str,
        raw_params: dict[str, Any],
        context: Any,
        *,
        subscription_manager: Any = None,
    ) -> dict[str, Any]:
        return {
            "method": method,
            "src_path": raw_params["src_path"],
            "dst_path": raw_params["dst_path"],
            "zone": context.zone_id or "root",
        }

    monkeypatch.setattr(
        "nexus.server._kernel_syscall_dispatch.dispatch_kernel_syscall",
        fake_dispatch_kernel_syscall,
    )
    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "zone_id": "root",
        "zone_perms": [["eng", "rw"], ["legal", "rw"]],
        "is_admin": False,
    }
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post(
            "/api/nfs/sys_copy",
            content=encode_rpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sys_copy",
                    "params": {
                        "src_path": "/zone/eng/a.txt",
                        "dst_path": "/zone/eng/b.txt",
                    },
                }
            ),
        )

    assert response.status_code == 200
    assert registry.zones == ["eng"]
    assert registry.runner.calls == 1
