"""Tests for permissions lifespan Tiger Cache startup."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.server.lifespan.permissions import _startup_tiger_cache
from nexus.server.lifespan.services import shutdown_services


@pytest.mark.asyncio
async def test_directory_grant_expander_starts_directly_inside_fastapi_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid kernel auto-start, which calls async services via asyncio.run()."""

    started: list[bool] = []

    class FakeDirectoryGrantExpander:
        def __init__(self, **_kwargs: object) -> None:
            self.stopped = False

        async def start(self) -> None:
            started.append(True)

    class FakeNexusFS:
        _kernel = object()

        def sys_setattr(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("async worker should not use kernel auto-start")

    import nexus.bricks.rebac.cache.tiger.expander as expander_module

    monkeypatch.setattr(
        expander_module,
        "DirectoryGrantExpander",
        FakeDirectoryGrantExpander,
    )
    monkeypatch.setenv("NEXUS_ENABLE_TIGER_WORKER", "false")

    tiger_cache = MagicMock()
    tiger_cache.warm_from_db.return_value = 0
    app = SimpleNamespace(state=SimpleNamespace(directory_grant_expander=None))
    svc = SimpleNamespace(
        nexus_fs=FakeNexusFS(),
        rebac_manager=SimpleNamespace(_tiger_cache=tiger_cache, engine=object()),
    )

    tasks = await _startup_tiger_cache(app, svc)
    try:
        assert started == [True]
        assert isinstance(app.state.directory_grant_expander, FakeDirectoryGrantExpander)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_shutdown_services_awaits_async_directory_grant_expander_stop() -> None:
    class FakeDirectoryGrantExpander:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    expander = FakeDirectoryGrantExpander()
    app = SimpleNamespace(
        state=SimpleNamespace(
            write_observer=None,
            zoekt_write_observer=None,
            task_dispatch_consumer=None,
            skeleton_pipe_consumer=None,
            workflow_dispatch=None,
            task_runner=None,
            scheduler_service=None,
            _eviction_task=None,
            sandbox_auth_service=None,
            search_daemon=None,
            directory_grant_expander=expander,
        )
    )
    svc = SimpleNamespace(nexus_fs=None)

    await shutdown_services(app, svc)

    assert expander.stopped is True
