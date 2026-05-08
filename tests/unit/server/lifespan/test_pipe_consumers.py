from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.server.lifespan.services import _startup_pipe_consumers


class _FakeTaskDispatchConsumer:
    def __init__(self) -> None:
        self.nx = None
        self.server_info: tuple[str, str] | None = None
        self.started = False

    def set_nx(self, nx: object) -> None:
        self.nx = nx

    def set_server_info(self, base_url: str, api_key: str) -> None:
        self.server_info = (base_url, api_key)

    async def start(self) -> None:
        self.started = True


@pytest.mark.asyncio
async def test_startup_pipe_consumers_passes_static_api_key_to_task_dispatch_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_HOST", raising=False)
    monkeypatch.delenv("NEXUS_PORT", raising=False)

    nx = object()
    consumer = _FakeTaskDispatchConsumer()
    app = SimpleNamespace(state=SimpleNamespace(agent_registry=None, api_key="server-secret"))
    svc = SimpleNamespace(
        nexus_fs=nx,
        write_observer=None,
        delivery_worker=None,
        event_signal=None,
        task_dispatch_consumer=consumer,
    )

    await _startup_pipe_consumers(app, svc)

    assert consumer.nx is nx
    assert consumer.server_info == ("http://127.0.0.1:2026", "server-secret")
    assert consumer.started is True
    assert app.state.task_dispatch_consumer is consumer
