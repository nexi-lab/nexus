from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.server.lifespan.services import _startup_pipe_consumers


class _FakeTaskDispatchConsumer:
    def __init__(self) -> None:
        self.nx = None
        self.started = False

    def set_nx(self, nx: object) -> None:
        self.nx = nx

    async def start(self) -> None:
        self.started = True


@pytest.mark.asyncio
async def test_startup_pipe_consumers_binds_and_starts_task_dispatch_consumer(
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
    assert consumer.started is True
    assert app.state.task_dispatch_consumer is consumer
