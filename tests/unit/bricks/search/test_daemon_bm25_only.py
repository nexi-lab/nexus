from __future__ import annotations

from typing import Any

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon


@pytest.mark.asyncio
async def test_bm25_only_startup_does_not_construct_txtai_backend(monkeypatch: pytest.MonkeyPatch):
    import nexus.bricks.search.txtai_backend as txtai_backend

    constructed = False

    class FakeTxtaiBackend:
        def __init__(self, **_: Any) -> None:
            nonlocal constructed
            constructed = True

        def kickoff_startup(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    monkeypatch.setattr(txtai_backend, "TxtaiBackend", FakeTxtaiBackend)

    daemon = SearchDaemon(
        DaemonConfig(
            database_url=None,
            txtai_model=None,
            vector_warmup_enabled=False,
            refresh_enabled=False,
            scope_refresh_seconds=0,
        )
    )

    try:
        await daemon.startup()

        assert constructed is False
        assert daemon._backend is None
        assert daemon.get_health()["backend"] == "legacy"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_configured_txtai_model_still_constructs_backend(monkeypatch: pytest.MonkeyPatch):
    import nexus.bricks.search.txtai_backend as txtai_backend

    constructed_with: dict[str, Any] = {}

    class FakeTxtaiBackend:
        def __init__(self, **kwargs: Any) -> None:
            constructed_with.update(kwargs)

        def kickoff_startup(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    monkeypatch.setattr(txtai_backend, "TxtaiBackend", FakeTxtaiBackend)

    daemon = SearchDaemon(
        DaemonConfig(
            database_url=None,
            txtai_model="openai/text-embedding-3-small",
            vector_warmup_enabled=False,
            refresh_enabled=False,
            scope_refresh_seconds=0,
        )
    )

    try:
        await daemon.startup()

        assert constructed_with["model"] == "openai/text-embedding-3-small"
        assert daemon._backend is not None
    finally:
        await daemon.shutdown()
